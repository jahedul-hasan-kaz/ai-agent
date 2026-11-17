"""
Retrieval over the backend's `docs` table (ai-agent-management owns it: content,
kind, tags, a pgvector `embedding`, tenant_id). Read only — documents are
written and embedded elsewhere.

Per chat message: embed the query with the same model that embedded the docs,
cosine-search the caller's tenant, keep matches above `min_similarity` and
within `max_similarity_gap` of the best one, at most `top_k`.
"""
import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
from uuid import UUID

import asyncpg
from loguru import logger
from openai import AsyncOpenAI

from app.llm.settings import RagConfig

Status = Literal["ok", "empty", "skipped", "error"]

# Re-try a failed startup (DB down, bad schema) at most this often, so a dead DB
# costs one cheap check per window instead of a slow connect on every message.
_RECONNECT_EVERY = 30.0

# Short follow-ups ("what about Saturday?") borrow the customer's previous message.
_SHORT_QUERY_WORDS = 6
_MAX_QUERY_CHARS = 2000
_PREVIEW_CHARS = 80

_SEARCH_SQL = """
SELECT id, kind, tags, content, 1 - (embedding <=> $1::text::vector) AS similarity
FROM public.docs
WHERE tenant_id = $2
  AND embedding IS NOT NULL
  AND ($4::text[] IS NULL OR kind = ANY($4::text[]))
ORDER BY embedding <=> $1::text::vector
LIMIT $3
"""

_COLUMN_TYPE_SQL = """
SELECT format_type(a.atttypid, a.atttypmod)
FROM pg_attribute a
WHERE a.attrelid = 'public.docs'::regclass AND a.attname = 'embedding' AND NOT a.attisdropped
"""

_STORED_DIMS_SQL = "SELECT vector_dims(embedding) FROM public.docs WHERE embedding IS NOT NULL LIMIT 1"


@dataclass
class RetrievedDoc:
    id: UUID
    kind: str
    tags: list[str]
    content: str
    similarity: float


@dataclass
class Retrieval:
    status: Status
    docs: list[RetrievedDoc] = field(default_factory=list)
    reason: str = ""


class Retriever:
    def __init__(self, settings: RagConfig, dsn: str) -> None:
        self.settings = settings
        self._dsn = dsn
        emb = settings.embedding
        self._embedder = AsyncOpenAI(
            base_url=emb.base_url,
            api_key=emb.api_key or "not-needed",
            timeout=emb.timeout,
            max_retries=emb.max_retries,
            default_headers=emb.headers or None,
        )
        self._pool: Optional[asyncpg.Pool] = None
        self._dims: Optional[int] = None
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._pool is not None and self._dims is not None

    async def start(self) -> None:
        """Connect and check `docs.embedding` against the embedding config. Never raises: chat works without RAG."""
        self._last_attempt = time.monotonic()
        try:
            if self._pool is None:
                # statement_cache_size=0: PG_DB_URL is Supavisor's transaction pooler, which can't keep prepared statements.
                self._pool = await asyncpg.create_pool(
                    self._dsn, min_size=1, max_size=5, statement_cache_size=0, command_timeout=self.settings.timeout
                )
            self._dims = await self._embedding_dims()
            logger.info(
                "RAG ready: docs.embedding has {} dims, embedding model {}, top_k={} min_similarity={} gap={} budget={}tok",
                self._dims, self.settings.embedding.model, self.settings.top_k, self.settings.min_similarity,
                self.settings.max_similarity_gap, self.settings.max_context_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            self._dims = None
            logger.error(
                "RAG unavailable, answering without the knowledge base: {} at {}. Check PG_DB_URL, the pgvector `docs.embedding` column and rag.embedding.",
                _describe(exc), _where(exc),
            )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        await self._embedder.close()

    async def retrieve(self, messages: list[dict], tenant_id: Optional[UUID]) -> Retrieval:
        rid = uuid.uuid4().hex[:6]
        log = logger.bind(rid=rid)
        if tenant_id is None:
            # docs are per tenant; searching all of them would leak one dealer's facts into another's chat.
            log.info("RAG[{}] skipped: no tenant_id on the request, model will be told it has no facts", rid)
            return Retrieval("skipped", reason="no tenant_id on the request")
        query = build_query(messages)
        if not query:
            log.info("RAG[{}] skipped tenant={}: no user text to search with", rid, tenant_id)
            return Retrieval("skipped", reason="no user text to search with")
        if not self.ready and not await self._reconnect():
            log.warning(
                "RAG[{}] unavailable tenant={}: DB/embedding check failed at startup, retrying at most every {}s (see the 'RAG unavailable' error above)",
                rid, tenant_id, int(_RECONNECT_EVERY),
            )
            return Retrieval("error", reason="knowledge base unavailable")

        log.info("RAG[{}] start tenant={} query={!r}{}", rid, tenant_id, _preview(query), " (+previous turn)" if "\n" in query else "")
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self._search(query, tenant_id, rid), self.settings.timeout)
        except asyncio.TimeoutError:
            log.warning("RAG[{}] timeout after {}s tenant={}: answering without context", rid, self.settings.timeout, tenant_id)
            return Retrieval("error", reason="timeout")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "RAG[{}] failed tenant={} after {}ms: {} at {}; answering without context",
                rid, tenant_id, _ms(started), _describe(exc), _where(exc),
            )
            return Retrieval("error", reason=type(exc).__name__)

        log.info(
            "RAG[{}] done tenant={} status={} kept={} total_ms={}{}",
            rid, tenant_id, result.status, len(result.docs), _ms(started), f" reason={result.reason}" if result.reason else "",
        )
        return result

    async def _search(self, query: str, tenant_id: UUID, rid: str) -> Retrieval:
        s = self.settings
        t = time.perf_counter()
        vector = await self._embed(query)
        embed_ms = _ms(t)

        t = time.perf_counter()
        # Over-fetch so duplicates and below-threshold rows can be dropped and still leave top_k.
        rows = await self._pool.fetch(_SEARCH_SQL, vector_literal(vector), tenant_id, s.top_k * 2, s.kinds)
        logger.info(
            "RAG[{}] searched embed_ms={} ({} dims) search_ms={} candidates={} kinds={}",
            rid, embed_ms, len(vector), _ms(t), len(rows), s.kinds or "all",
        )
        candidates = [RetrievedDoc(r["id"], r["kind"], list(r["tags"] or []), r["content"], float(r["similarity"])) for r in rows]
        docs = select_relevant(candidates, s.top_k, s.min_similarity, s.max_similarity_gap)

        if candidates:
            floor = max(s.min_similarity, candidates[0].similarity - s.max_similarity_gap)
            kept_ids = {d.id for d in docs}
            logger.debug("RAG[{}] similarity floor={:.3f} (min_similarity={}, best-{})", rid, floor, s.min_similarity, s.max_similarity_gap)
            for i, d in enumerate(candidates, 1):
                logger.debug(
                    "RAG[{}]   #{} {} sim={:.3f} kind={} id={} {!r}",
                    rid, i, "KEEP" if d.id in kept_ids else "drop", d.similarity, d.kind, d.id, _preview(d.content),
                )
                if d.id not in kept_ids:
                    logger.debug("RAG[{}]      reason: {}", rid, _drop_reason(d, candidates, floor, s.top_k, len(kept_ids)))

        if not docs:
            if not rows:
                reason = "no rows for this tenant with an embedding (check tenant_id, docs.embedding IS NOT NULL, rag.kinds)"
            else:
                reason = f"best match {rows[0]['similarity']:.3f} is below the floor, tune min_similarity or check the embedding model matches docs"
            return Retrieval("empty", reason=reason)
        return Retrieval("ok", docs)

    async def _embed(self, text: str) -> list[float]:
        emb = self.settings.embedding
        params = {"model": emb.model, "input": text}
        if emb.dimensions:
            params["dimensions"] = emb.dimensions
        vector = (await self._embedder.embeddings.create(**params)).data[0].embedding
        if len(vector) != self._dims:
            raise ValueError(f"{emb.model} returned {len(vector)} dims, docs.embedding has {self._dims}")
        return vector

    async def _embedding_dims(self) -> int:
        async with self._pool.acquire() as conn:
            column_type = await conn.fetchval(_COLUMN_TYPE_SQL)
            if column_type is None:
                raise RuntimeError("public.docs has no embedding column")
            match = re.fullmatch(r"vector(?:\((\d+)\))?", column_type)
            if not match:
                raise RuntimeError(f"docs.embedding is {column_type}, expected pgvector `vector`")
            dims = int(match.group(1)) if match.group(1) else await conn.fetchval(_STORED_DIMS_SQL)
        if dims is None:
            raise RuntimeError("docs has no embeddings yet")
        configured = self.settings.embedding.dimensions
        if configured and configured != dims:
            raise RuntimeError(f"rag.embedding.dimensions is {configured}, docs.embedding has {dims}")
        return dims

    async def embed_document(self, text: str) -> list[float]:
        """Embed text with the same model and dimensions the search uses (docs.embedding is only comparable to itself)."""
        return await self._embed(text)

    async def ensure_ready(self) -> bool:
        return self.ready or await self._reconnect()

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    async def _reconnect(self) -> bool:
        async with self._lock:
            if not self.ready and time.monotonic() - self._last_attempt >= _RECONNECT_EVERY:
                await self.start()
        return self.ready


def build_query(messages: list[dict]) -> str:
    """The latest customer message, plus the one before it when the latest is a short follow-up."""
    user_turns = [m["content"].strip() for m in messages if m["role"] == "user" and m["content"].strip()]
    if not user_turns:
        return ""
    query = user_turns[-1]
    if len(user_turns) > 1 and len(query.split()) < _SHORT_QUERY_WORDS:
        query = f"{user_turns[-2]}\n{query}"
    return query[-_MAX_QUERY_CHARS:]


def select_relevant(docs: list[RetrievedDoc], top_k: int, min_similarity: float, max_gap: float) -> list[RetrievedDoc]:
    """Rows come best first. Drop weak, off-cluster and duplicate matches; keep at most top_k."""
    if not docs:
        return []
    floor = max(min_similarity, docs[0].similarity - max_gap)
    kept, seen = [], set()
    for doc in docs:
        key = " ".join(doc.content.lower().split())
        if doc.similarity < floor or key in seen:
            continue
        seen.add(key)
        kept.append(doc)
        if len(kept) == top_k:
            break
    return kept


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _drop_reason(doc: RetrievedDoc, candidates: list[RetrievedDoc], floor: float, top_k: int, kept: int) -> str:
    if doc.similarity < floor:
        return f"{doc.similarity:.3f} < floor {floor:.3f}"
    key = " ".join(doc.content.lower().split())
    if any(c is not doc and c.similarity >= doc.similarity and " ".join(c.content.lower().split()) == key for c in candidates):
        return "duplicate content of a better match"
    return f"over top_k={top_k}" if kept >= top_k else "not selected"


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _PREVIEW_CHARS else flat[: _PREVIEW_CHARS - 1] + "…"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _describe(exc: BaseException) -> str:
    """`Type: message`, never the traceback: loguru's would print locals, including the DB URL and password."""
    return f"{type(exc).__name__}: {exc}"[:300]


def _where(exc: BaseException) -> str:
    """file:line of the innermost frame, enough to find the failing call."""
    tb = exc.__traceback__
    while tb and tb.tb_next:
        tb = tb.tb_next
    return f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}" if tb else "?"

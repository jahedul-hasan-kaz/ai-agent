"""
Create, update and delete `docs` rows, with the `embedding` column kept in step with the text.

Uses the retriever's Postgres pool and embedding model, so a row is always embedded
the way the search will embed the query. The embedding is generated first and the row
is written only if that worked: a doc is never stored without an embedding or with a
stale one. Datetimes and GUIDs are not embedded (see embedding_text.py).
"""
import asyncio
import json
import time
import uuid
from typing import Any, Optional
from uuid import UUID

import asyncpg
import openai
from loguru import logger

from app.llm.base import LLMError
from app.models.docs import DocCreate, DocOut, DocUpdate, Embedded
from app.rag.embedding_text import MAX_EMBED_CHARS, build_embedding_text
from app.rag.retriever import Retriever, vector_literal

_COLUMNS = "id, tenant_id, kind, tags, content, metadata::text AS metadata, image_path, cadence_id, updated_at"
_TEXT_FIELDS = {"kind", "content", "tags", "metadata"}  # a change to any of these needs a new embedding

_INSERT_SQL = f"""
INSERT INTO public.docs (id, tenant_id, kind, tags, content, embedding, metadata, image_path, cadence_id, updated_at)
VALUES ($1, $2, $3, $4::text[], $5, $6::text::vector, $7::text::jsonb, $8, $9, now() AT TIME ZONE 'utc')
RETURNING {_COLUMNS}
"""
_SELECT_SQL = f"SELECT {_COLUMNS} FROM public.docs WHERE id = $1 AND tenant_id = $2"
_DELETE_SQL = "DELETE FROM public.docs WHERE id = $1 AND tenant_id = $2"


class DocStore:
    def __init__(self, retriever: Retriever) -> None:
        self._r = retriever

    async def create(self, data: DocCreate) -> DocOut:
        tags = data.tags or []
        vector, embedded = await self._embed(data.kind, data.content, tags, data.metadata)
        doc_id = uuid.uuid4()
        started = time.perf_counter()
        row = await self._db(
            "create", self._r.pool.fetchrow, _INSERT_SQL,
            doc_id, data.tenant_id, data.kind, tags, data.content, vector_literal(vector),
            _json(data.metadata), data.image_path, data.cadence_id,
        )
        logger.info("DOCS create id={} tenant={} kind={} {} db_ms={}", doc_id, data.tenant_id, data.kind, _embed_log(embedded), _ms(started))
        return _out(row, embedded)

    async def update(self, doc_id: UUID, data: DocUpdate) -> DocOut:
        changes = data.model_dump(exclude_unset=True)
        tenant_id = changes.pop("tenant_id")
        if not changes:
            raise LLMError("Send at least one field to change.", 400, retryable=False, code="empty_update")
        await self._ready()

        current = await self._db("read", self._r.pool.fetchrow, _SELECT_SQL, doc_id, tenant_id)
        if current is None:
            raise _not_found(doc_id)

        merged = {
            "kind": current["kind"], "content": current["content"], "tags": list(current["tags"] or []),
            "metadata": _loads(current["metadata"]),
        }
        merged.update({k: v for k, v in changes.items() if k in _TEXT_FIELDS})
        merged["tags"] = merged["tags"] or []

        sets, args = [], [doc_id, tenant_id, current["updated_at"]]

        def add(template: str, value: Any) -> None:
            args.append(value)
            sets.append(template.format(n=len(args)))

        for column, template in (("kind", "kind = ${n}"), ("content", "content = ${n}"), ("image_path", "image_path = ${n}"),
                                 ("cadence_id", "cadence_id = ${n}")):
            if column in changes:
                add(template, changes[column])
        if "tags" in changes:
            add("tags = ${n}::text[]", merged["tags"])
        if "metadata" in changes:
            add("metadata = ${n}::text::jsonb", _json(merged["metadata"]))

        embedded: Optional[Embedded] = None
        if changes.keys() & _TEXT_FIELDS:
            vector, embedded = await self._embed(merged["kind"], merged["content"], merged["tags"], merged["metadata"])
            add("embedding = ${n}::text::vector", vector_literal(vector))
        sets.append("updated_at = now() AT TIME ZONE 'utc'")

        started = time.perf_counter()
        # The row must be unchanged since it was read; otherwise the new embedding could describe stale text.
        row = await self._db(
            "update", self._r.pool.fetchrow,
            f"UPDATE public.docs SET {', '.join(sets)} WHERE id = $1 AND tenant_id = $2 "
            f"AND updated_at IS NOT DISTINCT FROM $3 RETURNING {_COLUMNS}",
            *args,
        )
        if row is None:
            logger.warning("DOCS update id={} tenant={} conflict: the doc changed while it was being re-embedded", doc_id, tenant_id)
            raise LLMError("The doc was changed by someone else while updating. Retry.", 409, retryable=False, code="doc_conflict")
        logger.info(
            "DOCS update id={} tenant={} fields={} {} db_ms={}",
            doc_id, tenant_id, sorted(changes), _embed_log(embedded) if embedded else "embedding=unchanged", _ms(started),
        )
        return _out(row, embedded)

    async def delete(self, doc_id: UUID, tenant_id: UUID) -> None:
        await self._ready()
        status = await self._db("delete", self._r.pool.execute, _DELETE_SQL, doc_id, tenant_id)
        if status != "DELETE 1":
            raise _not_found(doc_id)
        logger.info("DOCS delete id={} tenant={}", doc_id, tenant_id)

    async def _ready(self) -> None:
        if not await self._r.ensure_ready():
            raise LLMError(
                "The knowledge base is unavailable (database or embedding check failed at startup). See the service log.",
                503, retryable=False, code="docs_unavailable",
            )

    async def _embed(self, kind: str, content: str, tags: list[str], metadata: Optional[dict]) -> tuple[list[float], Embedded]:
        await self._ready()
        full = build_embedding_text(kind, content, tags, metadata, max_chars=10**9)
        text = full[:MAX_EMBED_CHARS]
        started = time.perf_counter()
        try:
            vector = await self._r.embed_document(text)
        except asyncio.TimeoutError:
            raise LLMError("The embedding model timed out; nothing was saved.", 504, code="embedding_timeout") from None
        except openai.OpenAIError as exc:
            logger.warning("DOCS embedding failed: {}: {}", type(exc).__name__, str(exc)[:300])
            raise LLMError("The embedding model failed; nothing was saved.", 502, code="embedding_error") from None
        except ValueError as exc:  # wrong dimensions: rag.embedding doesn't match docs.embedding
            logger.error("DOCS embedding rejected: {}", exc)
            raise LLMError(f"Embedding model does not match docs.embedding: {exc}", 500, retryable=False, code="embedding_mismatch") from None
        embedded = Embedded(model=self._r.settings.embedding.model, dimensions=len(vector), chars=len(text), truncated=len(full) > len(text))
        if embedded.truncated:
            logger.warning("DOCS text is {} chars; embedded the first {}. The rest is stored but not searchable.", len(full), len(text))
        logger.debug("DOCS embedded in {}ms", _ms(started))
        return vector, embedded

    async def _db(self, action: str, call, *args):
        await self._ready()
        try:
            return await call(*args)
        except asyncpg.CheckViolationError as exc:
            raise LLMError(f"docs rejected the row: {exc.constraint_name or exc}", 400, retryable=False, code="invalid_doc") from None
        except asyncpg.ForeignKeyViolationError as exc:
            raise LLMError(
                f"tenant_id or cadence_id does not exist ({exc.constraint_name})", 400, retryable=False, code="invalid_reference"
            ) from None
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError) as exc:
            logger.warning("DOCS {} failed: {}: {}", action, type(exc).__name__, str(exc)[:300])
            raise LLMError(f"Database error while trying to {action} the doc.", 502, code="database_error") from None


def _out(row: asyncpg.Record, embedded: Optional[Embedded]) -> DocOut:
    return DocOut(
        id=row["id"], tenant_id=row["tenant_id"], kind=row["kind"], tags=list(row["tags"] or []), content=row["content"],
        metadata=_loads(row["metadata"]), image_path=row["image_path"], cadence_id=row["cadence_id"],
        updated_at=row["updated_at"], embedded=embedded,
    )


def _not_found(doc_id: UUID) -> LLMError:
    return LLMError(f"Doc {doc_id} not found for this tenant.", 404, retryable=False, code="doc_not_found")


def _json(value: Optional[dict]) -> Optional[str]:
    return None if value is None else json.dumps(value)


def _loads(value: Optional[str]) -> Optional[dict]:
    return None if value is None else json.loads(value)


def _embed_log(e: Embedded) -> str:
    return f"embedded={e.model}/{e.dimensions}d chars={e.chars}{' TRUNCATED' if e.truncated else ''}"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)

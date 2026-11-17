import json
import uuid
from datetime import datetime

import asyncpg
import openai
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import config
from app.llm.settings import AgentConfig, RagConfig
from app.llm.model_manager import ModelManager
from app.rag.embedding_text import MAX_EMBED_CHARS, build_embedding_text
from app.rag.store import DocStore
from tests.test_chat_completions import CONFIG, FakeProvider

TENANT = "6f1c2b0e-8a4d-4c3e-9b7a-1d2e3f4a5b6c"
DOC_ID = "0b0e9a42-77a3-4a5c-8f6e-2a6f3c5d9e10"
RAG = {"embedding": {"provider": "openai", "model": "text-embedding-3-small", "base_url": "https://api.openai.com/v1"}}
NOW = datetime(2026, 10, 27, 12, 0, 0)


def row(**over):
    base = dict(id=uuid.UUID(DOC_ID), tenant_id=uuid.UUID(TENANT), kind="faq", tags=["trade-in"], content="Appraisals take 15-20 minutes.",
                metadata=None, image_path=None, cadence_id=None, updated_at=NOW)
    base.update(over)
    return base


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.results: list = []  # popped in order: the next fetchrow/execute result

    def _next(self, kind, sql, args):
        self.calls.append((kind, " ".join(sql.split()), args))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def fetchrow(self, sql, *args):
        return self._next("fetchrow", sql, args)

    async def execute(self, sql, *args):
        return self._next("execute", sql, args)


class FakeRetriever:
    def __init__(self) -> None:
        self.settings = RagConfig.model_validate(RAG)
        self.pool = FakePool()
        self.texts: list[str] = []
        self.error: Exception | None = None
        self.ready = True

    async def ensure_ready(self):
        return self.ready

    async def embed_document(self, text):
        self.texts.append(text)
        if self.error:
            raise self.error
        return [0.25, -0.5, 1.0]


@pytest.fixture
def env(monkeypatch):
    async def no_retriever(_):
        return None

    monkeypatch.setattr(config, "load_agent_config", lambda: AgentConfig.model_validate(CONFIG))
    monkeypatch.setattr(main, "ModelManager", lambda cfg: ModelManager(cfg, FakeProvider))
    monkeypatch.setattr(main, "_start_retriever", no_retriever)
    monkeypatch.setattr(main, "_start_customers", no_retriever)
    retriever = FakeRetriever()
    with TestClient(main.app) as client:
        assert main.app.state.doc_store is None
        main.app.state.doc_store = DocStore(retriever)
        yield client, retriever


# --- what is embedded ---------------------------------------------------------------------------

def test_embedding_text_has_text_fields_but_no_guids_or_datetimes():
    text = build_embedding_text(
        "faq", "How long does an appraisal take?",
        tags=["trade-in", TENANT, "2026-10-27"],
        metadata={
            "source": "website", "price": 25000, "featured": True,
            "id": DOC_ID, "created_at": "2026-10-27T12:00:00Z", "valid_from": "2026-01-05 09:30",
            "nested": {"city": "Silver Spring", "ref": DOC_ID.replace("-", ""), "owner_id": TENANT},
            "models": ["Civic", DOC_ID, "2026-02-01"],
        },
    )
    assert text.startswith("kind: faq\n\ntags: trade-in\n\nHow long does an appraisal take?")
    for kept in ("source: website", "price: 25000", "featured: True", "nested.city: Silver Spring", "models: Civic"):
        assert kept in text
    assert DOC_ID not in text and TENANT not in text and DOC_ID.replace("-", "") not in text
    assert "2026-" not in text and "created_at" not in text and "owner_id" not in text


def test_embedding_text_keeps_dates_written_inside_content():
    assert "until 2026-12-31" in build_embedding_text("offer", "Offer valid until 2026-12-31.")


def test_embedding_text_is_capped():
    assert len(build_embedding_text("policy", "x" * 100_000)) == MAX_EMBED_CHARS


# --- create -------------------------------------------------------------------------------------

def _create(client, **over):
    body = {"tenant_id": TENANT, "kind": "faq", "content": "Appraisals take 15-20 minutes.", "tags": ["trade-in"],
            "metadata": {"source": "web", "created_at": "2026-10-27T12:00:00Z"}}
    body.update(over)
    return client.post("/v1/docs", json=body)


def test_create_embeds_text_and_stores_the_vector(env):
    client, retriever = env
    retriever.pool.results = [row(metadata=json.dumps({"source": "web"}))]
    res = _create(client)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["id"] == DOC_ID and data["metadata"] == {"source": "web"} and "embedding" not in data
    assert data["embedded"] == {"model": "text-embedding-3-small", "dimensions": 3, "chars": len(retriever.texts[0]), "truncated": False}
    assert "source: web" in retriever.texts[0] and "2026-10-27" not in retriever.texts[0]

    kind, sql, args = retriever.pool.calls[0]
    assert sql.startswith("INSERT INTO public.docs") and "$6::text::vector" in sql
    new_id, tenant, doc_kind, tags, content, vector, meta, image, cadence = args
    assert isinstance(new_id, uuid.UUID) and str(tenant) == TENANT and doc_kind == "faq" and tags == ["trade-in"]
    assert vector == "[0.25,-0.5,1.0]"
    assert json.loads(meta)["created_at"] == "2026-10-27T12:00:00Z"  # the datetime is stored in metadata, just not embedded


def test_create_saves_nothing_when_embedding_fails(env):
    client, retriever = env
    retriever.error = openai.APIConnectionError(request=None)
    res = _create(client)
    assert res.status_code == 502 and res.json()["error"]["code"] == "embedding_error"
    assert retriever.pool.calls == []


def test_create_reports_a_dimension_mismatch(env):
    client, retriever = env
    retriever.error = ValueError("text-embedding-3-small returned 1536 dims, docs.embedding has 768")
    res = _create(client)
    assert res.status_code == 500 and res.json()["error"]["code"] == "embedding_mismatch"
    assert retriever.pool.calls == []


@pytest.mark.parametrize("body", [{"kind": "blog"}, {"content": "   "}, {"tenant_id": "nope"}, {"tags": ["x"] * 101}])
def test_create_validation(env, body):
    client, _ = env
    assert _create(client, **body).status_code == 422


def test_unknown_tenant_or_cadence_is_a_400(env):
    client, retriever = env
    retriever.pool.results = [asyncpg.ForeignKeyViolationError("fk")]
    res = _create(client)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_reference"


def test_database_failure_is_a_502_without_internals(env):
    client, retriever = env
    retriever.pool.results = [asyncpg.PostgresConnectionError("password authentication failed for user x")]
    res = _create(client)
    assert res.status_code == 502 and "password" not in res.text


def test_docs_need_rag_configured(env):
    client, _ = env
    main.app.state.doc_store = None
    res = _create(client)
    assert res.status_code == 503 and res.json()["error"]["code"] == "docs_unavailable"


def test_docs_unavailable_when_startup_check_failed(env):
    client, retriever = env
    retriever.ready = False
    assert _create(client).status_code == 503


# --- update -------------------------------------------------------------------------------------

def _put(client, **body):
    body.setdefault("tenant_id", TENANT)
    return client.put(f"/v1/docs/{DOC_ID}", json=body)


def test_update_content_reembeds_with_the_existing_tags_and_metadata(env):
    client, retriever = env
    current = row(metadata=json.dumps({"source": "web"}))
    retriever.pool.results = [current, row(content="New answer.", metadata=json.dumps({"source": "web"}))]
    res = _put(client, content="New answer.")
    assert res.status_code == 200, res.text
    assert res.json()["embedded"]["dimensions"] == 3
    assert retriever.texts == ["kind: faq\n\ntags: trade-in\n\nNew answer.\n\nsource: web"]

    _, sql, args = retriever.pool.calls[1]
    assert "content = $4" in sql and "embedding = $5::text::vector" in sql and "updated_at IS NOT DISTINCT FROM $3" in sql
    assert args[3] == "New answer." and args[4] == "[0.25,-0.5,1.0]" and args[2] == NOW


def test_update_without_text_changes_keeps_the_embedding(env):
    client, retriever = env
    retriever.pool.results = [row(), row(image_path="uploads/a.png")]
    res = _put(client, image_path="uploads/a.png")
    assert res.status_code == 200 and res.json()["embedded"] is None
    assert retriever.texts == []
    assert "embedding" not in retriever.pool.calls[1][1]


def test_update_can_clear_tags_and_metadata(env):
    client, retriever = env
    retriever.pool.results = [row(metadata=json.dumps({"a": "b"})), row(tags=[])]
    assert _put(client, tags=None, metadata=None).status_code == 200
    assert retriever.texts == ["kind: faq\n\nAppraisals take 15-20 minutes."]


def test_update_not_found_and_conflict(env):
    client, retriever = env
    retriever.pool.results = [None]
    res = _put(client, kind="policy")
    assert res.status_code == 404 and res.json()["error"]["type"] == "not_found_error"

    retriever.pool.results = [row(), None]
    res = _put(client, kind="policy")
    assert res.status_code == 409 and res.json()["error"]["code"] == "doc_conflict"


@pytest.mark.parametrize("body, status", [({}, 400), ({"content": None}, 422), ({"kind": None}, 422), ({"kind": "blog"}, 422)])
def test_update_validation(env, body, status):
    client, retriever = env
    retriever.pool.results = [row()]
    assert _put(client, **body).status_code == status


# --- delete -------------------------------------------------------------------------------------

def test_delete_is_scoped_to_the_tenant(env):
    client, retriever = env
    retriever.pool.results = ["DELETE 1"]
    res = client.delete(f"/v1/docs/{DOC_ID}", params={"tenant_id": TENANT})
    assert res.status_code == 200 and res.json() == {"id": DOC_ID, "deleted": True}
    assert retriever.pool.calls[0][1] == "DELETE FROM public.docs WHERE id = $1 AND tenant_id = $2"

    retriever.pool.results = ["DELETE 0"]
    assert client.delete(f"/v1/docs/{DOC_ID}", params={"tenant_id": TENANT}).status_code == 404
    assert client.delete(f"/v1/docs/{DOC_ID}").status_code == 422


# --- only tenant_id, kind and content are required ------------------------------------------------

def test_create_with_only_the_required_fields(env):
    client, retriever = env
    retriever.pool.results = [row(tags=[], metadata=None)]
    res = client.post("/v1/docs", json={"tenant_id": TENANT, "kind": "faq", "content": "Hello"})
    assert res.status_code == 201, res.text
    assert res.json()["tags"] == [] and res.json()["metadata"] is None
    _, _, args = retriever.pool.calls[0]
    assert args[3] == [] and args[6] is None and args[7] is None and args[8] is None  # tags, metadata, image_path, cadence_id
    assert retriever.texts == ["kind: faq\n\nHello"]


def test_create_treats_null_and_blank_optionals_as_not_set(env):
    client, retriever = env
    retriever.pool.results = [row()]
    res = client.post("/v1/docs", json={"tenant_id": TENANT, "kind": "faq", "content": "Hello", "tags": None,
                                        "metadata": None, "image_path": "", "cadence_id": "  "})
    assert res.status_code == 201, res.text
    _, _, args = retriever.pool.calls[0]
    assert args[3] == [] and args[7] is None and args[8] is None


@pytest.mark.parametrize("missing", ["tenant_id", "kind", "content"])
def test_create_requires_tenant_kind_and_content(env, missing):
    client, _ = env
    body = {"tenant_id": TENANT, "kind": "faq", "content": "Hello"}
    body.pop(missing)
    res = client.post("/v1/docs", json=body)
    assert res.status_code == 422 and missing in res.json()["error"]["message"]


def test_update_needs_only_tenant_id_plus_one_field_and_blank_clears_optionals(env):
    client, retriever = env
    retriever.pool.results = [row(image_path="a.png"), row(image_path=None)]
    res = _put(client, image_path="", cadence_id="")
    assert res.status_code == 200, res.text
    _, sql, args = retriever.pool.calls[1]
    assert "image_path = $4" in sql and "cadence_id = $5" in sql and args[3] is None and args[4] is None
    assert retriever.texts == []  # no text field changed, so no new embedding

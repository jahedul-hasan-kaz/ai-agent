import json
import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api.v1.endpoints import playground as endpoints
from app.core.config import config
from app.llm.model_manager import ModelManager
from app.llm.settings import AgentConfig
from tests.test_chat_completions import CONFIG, TENANT, FakeProvider
from tests.test_customer import FakeStore, _records

UI = "http://localhost:5173"


def _client(monkeypatch, playground):
    async def none(_):
        return None

    monkeypatch.setattr(config, "load_agent_config", lambda: AgentConfig.model_validate({**CONFIG, "playground": playground}))
    monkeypatch.setattr(main, "ModelManager", lambda cfg: ModelManager(cfg, FakeProvider))
    monkeypatch.setattr(main, "_start_retriever", none)
    monkeypatch.setattr(main, "_start_customers", none)
    return TestClient(main.app)


@pytest.fixture
def enabled(monkeypatch):
    async def pool(_):
        return object()

    calls = []

    async def threads(pool, tenant_id, channel, search, limit, customer=None, text=None):
        calls.append((tenant_id, channel, search, limit) + ((customer, text) if customer or text else ()))
        return [{"key": "+15551234567", "name": "Mychal Reed", "last_at": "2026-01-05T15:00:00Z",
                 "last_direction": "inbound", "last_message": "hi"}]

    monkeypatch.setattr(endpoints, "_pool", pool)
    monkeypatch.setattr(endpoints.playground, "threads", threads)
    with _client(monkeypatch, {"enabled": True, "cors_origins": [UI]}) as c:
        main.app.state.chat_service.customers = FakeStore(_records())
        yield c, calls


def test_off_by_default(monkeypatch):
    with _client(monkeypatch, {}) as c:
        assert c.get(f"/v1/playground/threads?tenant_id={TENANT}").status_code == 404
        assert c.options("/v1/playground/models", headers={"Origin": UI, "Access-Control-Request-Method": "GET"}) \
            .headers.get("access-control-allow-origin") is None


def test_threads_need_no_api_key_and_allow_the_ui_origin(enabled):
    c, calls = enabled
    res = c.get(f"/v1/playground/threads?tenant_id={TENANT}&channel=email&search=reed", headers={"Origin": UI})
    assert res.status_code == 200 and res.json()["data"][0]["name"] == "Mychal Reed"
    assert res.headers["access-control-allow-origin"] == UI
    assert calls == [(uuid.UUID(TENANT), "email", "reed", 50)]
    assert c.get("/v1/playground/threads").status_code == 200  # every dealership
    assert calls[-1] == (None, "sms", None, 50)
    assert c.get(f"/v1/playground/threads?tenant_id={TENANT}&channel=fax").status_code == 422


def test_threads_pass_customer_and_text_filters(enabled):
    c, calls = enabled
    res = c.get(f"/v1/playground/threads?tenant_id={TENANT}&customer=(555) 123&text=trade in")
    assert res.status_code == 200
    assert calls == [(uuid.UUID(TENANT), "sms", None, 50, "(555) 123", "trade in")]
    assert c.get("/v1/playground/threads?text=" + "x" * 201).status_code == 422


class _Pool:
    def __init__(self, rows):
        self.rows, self.sql, self.args = rows, None, None

    async def fetch(self, sql, *args):
        self.sql, self.args = sql, args
        return self.rows


async def test_sms_filters_reach_the_query_escaped_and_the_preview_shows_the_match():
    row = {"tenant_id": TENANT, "tenant_name": "Kia", "key": "+15551234567", "name": None,
           "last_at": "2026-01-05T15:00:00Z", "direction": "inbound",
           "body": "Hello " + "x" * 200 + " what is my trade-in worth?"}
    pool = _Pool([row])
    await endpoints.playground.threads(pool, None, "sms", None, 10, customer=" (555) 123 ", text="trade-IN 50%")
    assert pool.args == (None, 10, None, "(555) 123", "555123", "trade-IN 50\\%")
    assert "regexp_replace(last.key" in pool.sql and "m.message ILIKE" in pool.sql

    pool = _Pool([row])
    out = await endpoints.playground.threads(pool, None, "sms", None, 10, text="TRADE-in")
    assert out[0]["last_message"].startswith("…") and "trade-in worth?" in out[0]["last_message"]
    assert pool.args == (None, 10, None, None, None, "TRADE-in")


async def test_email_filters_have_no_digits_argument():
    pool = _Pool([])
    await endpoints.playground.threads(pool, None, "email", "kia", 10, customer="reed@", text="")
    assert pool.args == (None, 10, "kia", "reed@", None)


def test_models_and_streaming_chat(enabled):
    c, _ = enabled
    assert [m["id"] for m in c.get("/v1/playground/models").json()["data"]] == ["claude", "gpt", "llama"]
    body = {"model": "llama", "tenant_id": TENANT, "sms_thread_key": "+15551234567", "stream": True,
            "messages": [{"role": "user", "content": "What's my Telluride worth?"}]}
    with c.stream("POST", "/v1/playground/chat/completions", json=body) as res:
        lines = [line for line in res.iter_lines() if line]
    assert lines[-1] == "data: [DONE]"
    text = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content", "") for l in lines[:-1])
    assert text.strip() == "Reply from llama"

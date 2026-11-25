import json
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import ConfigError, config
from app.llm.anthropic_provider import _alternating_turns
from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResult, StreamDone, StreamItem
from app.llm.model_manager import ModelManager
from app.customer.context import build_query
from app.knowledge.base import entries, knowledge_base_system, search
from app.llm.settings import AgentConfig, ModelConfig
from app.services.agent_prompts import AGENTS_DIR, agents, prepare, runtime_context

CONFIG = {
    "models": [
        {"id": "claude", "type": "frontier", "provider": "anthropic", "protocol": "anthropic", "model": "claude-sonnet-5-5",
         "api_key": "k", "base_url": "https://api.anthropic.com", "temperature": 0.3, "fallbacks": ["gpt"]},
        {"id": "gpt", "type": "frontier", "provider": "openai", "protocol": "openai", "model": "gpt-4.1-mini", "api_key": "k",
         "base_url": "https://api.openai.com/v1"},
        {"id": "llama", "type": "opensource", "provider": "ollama", "protocol": "openai", "model": "llama3.2:1b",
         "base_url": "http://localhost:11434/v1/", "max_tokens": 256},
    ],
}


class FakeProvider(LLMProvider):
    def __init__(self, settings: ModelConfig) -> None:
        super().__init__(settings)
        self.reply = f"Reply from {settings.id}"
        self.error: LLMError | None = None
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self.error:
            raise self.error
        return LLMResult(self.reply, "stop", 12, 8)

    async def list_models(self) -> list[str]:
        return [self.settings.model]

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamItem]:
        self.requests.append(request)
        if self.error:
            raise self.error
        for word in self.reply.split(" "):
            yield word + " "
        yield StreamDone("stop", 12, 8)


@pytest.fixture
def providers():
    return {}


@pytest.fixture
def client(providers, monkeypatch):
    def factory(settings):
        providers[settings.id] = FakeProvider(settings)
        return providers[settings.id]

    monkeypatch.setattr(config, "load_agent_config", lambda: AgentConfig.model_validate(CONFIG))
    monkeypatch.setattr(main, "ModelManager", lambda cfg: ModelManager(cfg, factory))
    monkeypatch.setattr(main, "_start_customers", _no_store)
    with TestClient(main.app) as c:
        yield c


async def _no_store(_):
    return None


def _post(client, **body):
    body.setdefault("messages", [{"role": "user", "content": "Is the 2021 Civic still available?"}])
    return client.post("/v1/chat/completions", json=body)


def test_named_model(client, providers):
    res = _post(client, model="claude")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["model"] == "claude" and data["model_type"] == "frontier"
    assert data["choices"][0]["message"] == {"role": "assistant", "content": "Reply from claude"}
    assert data["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    sent = providers["claude"].requests[0]
    general = agents()["general"].text
    assert sent.messages[0]["role"] == "system" and sent.messages[0]["content"].startswith(general)
    # Cached prefix: the agent prompt, its addendum and the static knowledge base.
    assert sent.cache_prefix == sent.messages[0]["content"].index(knowledge_base_system()) + len(knowledge_base_system())
    # config defaults fill what the caller left unset
    assert sent.temperature == 0.3 and sent.max_tokens == 1000


def test_missing_model_lists_choices(client):
    res = _post(client)
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "model_required"
    assert error["message"] == (
        "No model specified. Set `model` to one of: claude (frontier), gpt (frontier), llama (opensource)."
    )


def test_missing_model_with_type_lists_that_type(client):
    res = _post(client, model_type="opensource")
    assert res.json()["error"]["message"] == "No model specified. Set `model` to one of: llama (opensource)."


def test_missing_model_streaming_is_plain_400(client):
    status, _ = _stream(client)
    assert status == 400


def test_model_config_fills_unset_params(client, providers):
    _post(client, model="llama")
    assert providers["llama"].requests[0].max_tokens == 256


def test_request_params_override_config(client, providers):
    _post(client, model="claude", temperature=0.9, max_tokens=50)
    sent = providers["claude"].requests[0]
    assert (sent.temperature, sent.max_tokens) == (0.9, 50)


def test_caller_system_message_follows_default_prompt(client, providers):
    _post(client, model="claude", messages=[{"role": "system", "content": "Dealer is Beechmont Ford."},
                            {"role": "user", "content": "hi"}])
    system = providers["claude"].requests[0].messages[0]["content"]
    assert system.endswith("Dealer is Beechmont Ford.")


def test_unknown_model(client):
    res = _post(client, model="nope")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "unknown_model"


def test_model_type_mismatch(client):
    res = _post(client, model="llama", model_type="frontier")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "model_type_mismatch"


def test_fallback_on_upstream_failure(client, providers):
    providers["claude"].error = LLMError("anthropic returned 529: overloaded", 502)
    data = _post(client, model="claude").json()
    assert data["model"] == "gpt" and data["fallback_used"] is True


def test_no_fallback_on_bad_request(client, providers):
    providers["claude"].error = LLMError("bad params", 400, retryable=False, code="invalid_request")
    res = _post(client, model="claude")
    assert res.status_code == 400
    assert providers["gpt"].requests == []


def test_all_candidates_fail(client, providers):
    providers["claude"].error = LLMError("timed out", 504, code="provider_timeout")
    providers["gpt"].error = LLMError("timed out", 504, code="provider_timeout")
    res = _post(client, model="claude")
    assert res.status_code == 504
    assert res.json()["error"]["type"] == "upstream_error"


def test_validation_error_shape(client):
    res = client.post("/v1/chat/completions", json={"messages": [{"role": "assistant", "content": "hi"}]})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"


def _stream(client, **body):
    body.update(stream=True, messages=[{"role": "user", "content": "hello"}])
    with client.stream("POST", "/v1/chat/completions", json=body) as res:
        return res.status_code, [l for l in res.iter_lines() if l.startswith("data: ")]


def test_streaming(client):
    status, lines = _stream(client, model="llama")
    assert status == 200 and lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks).strip() == "Reply from llama"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 20


def test_streaming_falls_back_before_first_token(client, providers):
    providers["claude"].error = LLMError("overloaded", 502)
    _, lines = _stream(client, model="claude")
    final = json.loads(lines[-2][6:])
    assert final["model"] == "gpt" and final["fallback_used"] is True


def test_streaming_unknown_model_is_plain_400(client):
    status, _ = _stream(client, model="nope")
    assert status == 400


def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setattr(main.app.state.chat_service.manager.config, "service_api_key", "secret-key")
    assert _post(client, model="claude").status_code == 401
    ok = client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret-key"},
                     json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]})
    assert ok.status_code == 200


def test_models_listing_hides_keys(client):
    data = client.get("/v1/models").json()["data"]
    assert [m["id"] for m in data] == ["claude", "gpt", "llama"]
    assert all("api_key" not in m for m in data)


@pytest.mark.parametrize("change, message", [
    (lambda c: c["models"][1].update(protocol="grpc"), "protocol"),
    (lambda c: c["models"][1].pop("protocol"), "protocol"),
    (lambda c: c["models"][2].pop("base_url"), "base_url"),
    (lambda c: c["models"][2].update(base_url="localhost:11434"), "must start with http"),
    (lambda c: c["models"][2].update(id="claude"), "duplicate model id"),
    (lambda c: c["models"][0].update(fallbacks=["missing"]), "invalid fallbacks"),
])
def test_config_validation(change, message):
    cfg = json.loads(json.dumps(CONFIG))
    change(cfg)
    with pytest.raises(ValueError, match=message):
        AgentConfig.model_validate(cfg)


def test_dev_config_errors_are_readable(monkeypatch):
    monkeypatch.setenv("AI_AGENT_CONFIG", '{"models": []}')
    with pytest.raises(ConfigError, match="models: List should have at least 1 item"):
        config.load_agent_config()
    monkeypatch.setenv("AI_AGENT_CONFIG", "{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        config.load_agent_config()


def test_anthropic_turns_alternate():
    turns = _alternating_turns([
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "Want a value on your Fusion?"},
        {"role": "user", "content": "Yes"},
        {"role": "user", "content": "How much?"},
    ])
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[2]["content"] == "Yes\n\nHow much?"


def test_every_agent_prompt_is_loaded_and_rendered():
    loaded = agents()
    assert list(loaded)[0] == "general"
    assert set(loaded) == {"general", "inventory", "scheduling", "service_trade_reply", "value_data_collection"}
    for agent in loaded.values():
        assert "{{" not in agent.text and "$('" not in agent.text, agent.id
        assert "__INTERNAL" not in agent.text and "\n\n\n" not in agent.text
    general = loaded["general"].text
    assert "Name: the dealership" in general
    assert "Current date: (see current date under RUNTIME CONTEXT)" in general


def test_prompt_files_are_not_modified():
    path = AGENTS_DIR / "general_system_instr.md"
    before = path.read_bytes()
    agents.cache_clear()
    agents()
    assert path.read_bytes() == before


def test_render_n8n_expressions():
    assert prepare("Hi {{ $('Lookup Lead').first().json.first_name ?? 'there' }}!") == "Hi there!"
    assert prepare("{{ $('X').isExecuted ? ($('X').first().json.a ?? 'none') : 'none' }}") == "none"
    assert prepare("{{ $('M').first().json.v ?? '__INTERNAL_NULL_VALUE_TRIPWIRE__' }}") == "unavailable"
    assert prepare("{{ (() => { return 1; })() }}") == "(not provided)"
    assert prepare("a  \n\n\n\nb") == "a\n\nb"


def test_runtime_context_uses_dealer_timezone():
    from datetime import datetime, timezone

    block = runtime_context(datetime(2026, 11, 13, 14, 5, tzinfo=timezone.utc))
    assert "Current date: 2026-11-13" in block and "Current time: 9:05 AM (America/New_York)" in block
    assert "Day of week: Friday" in block


def test_agent_selection(client, providers):
    _post(client, model="gpt", agent="inventory")
    assert providers["gpt"].requests[0].messages[0]["content"].startswith(agents()["inventory"].text)


@pytest.mark.parametrize("sent, expected", [
    ("inventory", "inventory"), ("scheduling", "scheduling"), ("schedule", "scheduling"),
    ("value_data_collection", "value_data_collection"), ("Value Data-Collection", "value_data_collection"),
    ("service_trade_reply", "service_trade_reply"), ("general", "general"), (None, "general"), ("", "general"),
])
def test_agent_payload_names_map_to_prompts(client, providers, sent, expected):
    res = _post(client, model="gpt", agent=sent)
    assert res.status_code == 200, res.text
    assert providers["gpt"].requests[0].messages[0]["content"].startswith(agents()[expected].text)


def test_scheduling_uses_the_schedule_file():
    assert agents()["scheduling"].text == prepare((AGENTS_DIR / "schedule_system_instr.md").read_text(encoding="utf-8"))


def test_unknown_agent_lists_choices(client):
    res = _post(client, model="gpt", agent="nope")
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "unknown_agent" and "general" in error["message"]


def test_agents_listing(client):
    data = client.get("/v1/agents").json()["data"]
    assert data[0]["id"] == "general" and data[0]["default"] is True
    assert all(a["prompt_tokens"] > 1000 for a in data)


def _windowed_client(providers, monkeypatch, window, max_tokens=512):
    cfg = json.loads(json.dumps(CONFIG))
    cfg["models"][2].update(context_window=window, max_tokens=max_tokens)

    def factory(settings):
        providers[settings.id] = FakeProvider(settings)
        return providers[settings.id]

    monkeypatch.setattr(config, "load_agent_config", lambda: AgentConfig.model_validate(cfg))
    monkeypatch.setattr(main, "ModelManager", lambda c: ModelManager(c, factory))
    monkeypatch.setattr(main, "_start_customers", _no_store)
    return TestClient(main.app)


def test_old_turns_are_dropped_to_fit_the_window(providers, monkeypatch):
    agent_tokens = agents()["general"].tokens
    turns = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "word " * 400} for i in range(40)]
    turns.append({"role": "user", "content": "latest question"})
    with _windowed_client(providers, monkeypatch, agent_tokens + 6000) as c:
        res = _post(c, model="llama", messages=turns)
    assert res.status_code == 200, res.text
    sent = providers["llama"].requests[0].messages
    assert sent[-1]["content"] == "latest question"
    assert 1 < len(sent) < len(turns)  # newest turns kept, oldest dropped
    assert sent[-2]["content"].startswith("turn 39")
    assert sum(len(m["content"]) for m in sent) / 3.5 < agent_tokens + 6000 - 512


def test_window_too_small_for_agent_is_a_clear_error(providers, monkeypatch):
    with _windowed_client(providers, monkeypatch, 4096) as c:
        res = _post(c, model="llama")
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "context_window_exceeded" and "OLLAMA_CONTEXT_LENGTH" in error["message"]
    assert not providers["llama"].requests


def test_anthropic_caches_the_agent_prompt():
    from app.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(ModelConfig.model_validate(CONFIG["models"][0]))
    request = LLMRequest(messages=[{"role": "system", "content": "AGENT\n\nruntime"}, {"role": "user", "content": "hi"}],
                         cache_prefix=len("AGENT"))
    assert provider._params(request)["system"] == [
        {"type": "text", "text": "AGENT", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "runtime"},
    ]
    request.cache_prefix = 0
    assert provider._params(request)["system"] == "AGENT\n\nruntime"


TENANT = "6f1c2b0e-8a4d-4c3e-9b7a-1d2e3f4a5b6c"


@pytest.mark.parametrize("agent", ["general", "inventory", "scheduling", "value_data_collection", "service_trade_reply"])
def test_every_agent_gets_the_static_knowledge_base(client, providers, agent):
    res = _post(client, model="claude", tenant_id=TENANT, agent=agent,
                messages=[{"role": "system", "content": "Dealer is Beechmont Ford."},
                          {"role": "user", "content": "How long does a trade-in appraisal take?"}])
    assert res.status_code == 200, res.text
    request = providers["claude"].requests[0]
    system = request.messages[0]["content"]
    kb = knowledge_base_system()
    # Static, so it sits in the cached prefix, before the per-request runtime block and caller messages.
    assert system.index(kb) < system.index("=== RUNTIME CONTEXT ===") < system.index("Beechmont Ford")
    assert request.cache_prefix == system.index(kb) + len(kb)
    assert "[faq] tags: trade-in, process, timing\nQ: How long does a trade-in appraisal take?" in system
    assert "Reply STOP to opt out." in system and "Koons Lincoln of Bethesda is 301-890-5100" in system


def test_knowledge_base_needs_no_tenant_or_database(client, providers):
    assert _post(client, model="llama").status_code == 200
    assert knowledge_base_system() in providers["llama"].requests[0].messages[0]["content"]


def test_streaming_gets_the_knowledge_base(client, providers):
    status, lines = _stream(client, model="llama")
    assert status == 200 and lines[-1] == "data: [DONE]"
    assert "[policy] tags: return, satisfaction, guarantee" in providers["llama"].requests[0].messages[0]["content"]


def test_every_distinct_docs_row_is_an_entry():
    docs = entries()
    assert len(docs) == 32  # 37 rows in docs, minus 5 exact copies
    assert [d.kind for d in docs].count("faq") == 26
    assert [d.kind for d in docs].count("policy") == 4
    footers = [d for d in docs if d.kind == "compliance"]
    assert [d.content for d in footers] == ["UPDATED: Frequently asked questions and answers", "Reply STOP to opt out."]
    assert footers[0].metadata == {"cadence_name": "Trade Agent AI Outbound", "template_key": "compliance_footer"}
    first = docs[0]
    assert first.tags == ["store", "address"] and first.content.startswith("Q: Where are you located?\nA: Sheehy Volkswagen")
    assert all(d.content for d in docs)


def test_build_query_borrows_previous_turn_for_short_follow_ups():
    turns = [{"role": "user", "content": "Do you take leased cars as trade-ins?"},
             {"role": "assistant", "content": "Yes!"},
             {"role": "user", "content": "And older ones?"}]
    assert build_query(turns) == "Do you take leased cars as trade-ins?\nAnd older ones?"
    turns[-1]["content"] = "What do I need to bring for the trade-in appraisal on Saturday?"
    assert build_query(turns) == turns[-1]["content"]


def test_search_ranks_by_shared_words_and_tags():
    phone = search(entries(), "what's your phone number?", 3)
    assert phone and "301-890-6100" in phone[0][0].content
    assert search(entries(), "can I trade in my lease", 3)[0][0].tags == ["lease", "lease-end", "buyout"]
    assert search(entries(), "the", 3) == []


def test_base_url_comes_from_config(client, providers):
    assert providers["llama"].settings.base_url == "http://localhost:11434/v1"  # trailing slash trimmed
    assert providers["claude"].settings.base_url == "https://api.anthropic.com"


def test_any_vendor_label_works_with_a_protocol():
    """No vendor list in code: a new server only needs a config entry."""
    from app.llm.model_manager import build_provider
    from app.llm.openai_compatible import OpenAICompatibleProvider

    entry = ModelConfig.model_validate({
        "id": "qwen", "type": "opensource", "provider": "lm-studio", "protocol": "openai",
        "model": "qwen2.5-7b", "base_url": "http://localhost:1234/v1",
    })
    assert isinstance(build_provider(entry), OpenAICompatibleProvider)


def test_remote_models_one_query_per_server(client, providers, monkeypatch):
    calls = []

    async def listing(self):
        calls.append(self.settings.base_url)
        if self.settings.id == "gpt":
            raise LLMError("Could not reach openai", 503, code="provider_unreachable")
        return [f"{self.settings.id}-a", f"{self.settings.id}-b"]

    monkeypatch.setattr(FakeProvider, "list_models", listing, raising=False)
    data = client.get("/v1/models/remote").json()["data"]
    by_url = {d["base_url"]: d for d in data}
    assert by_url["http://localhost:11434/v1"]["models"] == ["llama-a", "llama-b"]
    assert by_url["https://api.openai.com/v1"] == {
        "provider": "openai", "protocol": "openai", "base_url": "https://api.openai.com/v1",
        "models": [], "error": "Could not reach openai",
    }
    assert len(calls) == 3


def test_failed_fallback_chain_reports_every_attempt(client, providers):
    providers["claude"].error = LLMError("ollama does not serve this model: claude gone", 502, code="model_not_found")
    providers["gpt"].error = LLMError("ollama does not serve this model: gpt gone", 502, code="model_not_found")
    res = _post(client, model="claude")
    assert res.status_code == 502
    assert res.json()["error"]["message"] == (
        "All 2 models failed — claude: ollama does not serve this model: claude gone; "
        "gpt: ollama does not serve this model: gpt gone"
    )


def test_extra_body_is_passed_through():
    from app.llm.anthropic_provider import AnthropicProvider
    from app.llm.openai_compatible import OpenAICompatibleProvider

    entry = {"id": "q", "type": "opensource", "provider": "ollama", "protocol": "openai", "model": "qwen",
             "base_url": "http://localhost:11434/v1", "extra_body": {"reasoning_effort": "none"}}
    request = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    params = OpenAICompatibleProvider(ModelConfig.model_validate(entry))._params(request)
    assert params["extra_body"] == {"reasoning_effort": "none"}

    entry.update(protocol="anthropic", base_url="https://api.anthropic.com", extra_body={})
    assert "extra_body" not in AnthropicProvider(ModelConfig.model_validate(entry))._params(request)


def _wire_body(provider_cls, entry, request):
    """Send `request` through the real SDK and return the JSON body it puts on the wire."""
    import asyncio

    import httpx

    seen = {}

    def handler(http_request):
        seen["body"] = json.loads(http_request.content)
        if "anthropic" in str(http_request.url):
            return httpx.Response(200, json={
                "id": "m", "type": "message", "role": "assistant", "model": "x", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}})
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "x",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    provider = provider_cls(ModelConfig.model_validate(entry))
    provider._client = provider._client.with_options(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(provider.complete(request))
    return seen["body"]


OLLAMA_ENTRY = {"id": "q", "type": "opensource", "provider": "ollama", "protocol": "openai", "model": "qwen",
                "base_url": "http://ollama.test/v1", "extra_body": {"reasoning_effort": "none", "keep_alive": "10m"}}
CLAUDE_ENTRY = {"id": "c", "type": "frontier", "provider": "anthropic", "protocol": "anthropic",
                "model": "claude-sonnet-5-5", "api_key": "k", "base_url": "https://api.anthropic.test",
                "extra_body": {"output_config": {"effort": "high", "task_budget": None}}}


def test_reasoning_effort_reaches_ollama_and_overrides_config():
    from app.llm.openai_compatible import OpenAICompatibleProvider

    hi = LLMRequest(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")
    body = _wire_body(OpenAICompatibleProvider, OLLAMA_ENTRY, hi)
    assert body["reasoning_effort"] == "high" and body["keep_alive"] == "10m"

    unset = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    assert _wire_body(OpenAICompatibleProvider, OLLAMA_ENTRY, unset)["reasoning_effort"] == "none"  # config default


def test_reasoning_effort_maps_to_claude_effort_without_sampling_params():
    from app.llm.anthropic_provider import AnthropicProvider

    request = LLMRequest(messages=[{"role": "user", "content": "hi"}], reasoning_effort="none",
                         temperature=0.4, top_p=0.9, top_k=40)
    body = _wire_body(AnthropicProvider, CLAUDE_ENTRY, request)
    assert body["output_config"] == {"effort": "low", "task_budget": None}  # no "none" on Claude
    assert "temperature" not in body and "top_p" not in body and "top_k" not in body


def test_reasoning_effort_flows_from_api(client, providers):
    _post(client, model="llama", reasoning_effort="medium")
    assert providers["llama"].requests[0].reasoning_effort == "medium"
    bad = _post(client, model="llama", reasoning_effort="extreme")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"


def test_sampling_params_flow_to_openai_compatible_body(client, providers):
    _post(client, model="llama", temperature=0.2, top_p=0.5, top_k=20)
    sent = providers["llama"].requests[0]
    assert (sent.temperature, sent.top_p, sent.top_k) == (0.2, 0.5, 20)

    from app.llm.openai_compatible import OpenAICompatibleProvider

    request = LLMRequest(messages=[{"role": "user", "content": "hi"}], temperature=0.2, top_p=0.5, top_k=20)
    body = _wire_body(OpenAICompatibleProvider, OLLAMA_ENTRY, request)
    assert body["temperature"] == 0.2 and body["top_p"] == 0.5 and body["top_k"] == 20

    assert _post(client, model="llama", top_k=0).status_code == 422

"""The truncation guard: a server reporting far fewer prompt tokens than were sent cut the prompt (Ollama's
default context length does this silently), and the gateway says so in the log."""
import asyncio

import pytest
from loguru import logger

from app.llm.base import LLMProvider, LLMRequest, LLMResult, StreamDone, ToolSpec
from app.llm.model_manager import ModelManager, sent_tokens, warn_if_truncated
from app.llm.settings import AgentConfig, ModelConfig

PROMPT = "x" * 28_000  # ~8,000 tokens by the gateway's estimate


def model(**overrides) -> ModelConfig:
    fields = dict(id="oss", type="opensource", provider="ollama", protocol="openai", model="gpt-oss:20b",
                  base_url="http://localhost:11434/v1", context_window=16384)
    return ModelConfig(**{**fields, **overrides})


def request(text: str = PROMPT, **kw) -> LLMRequest:
    return LLMRequest(messages=[{"role": "system", "content": text}, {"role": "user", "content": "hi"}], **kw)


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    yield seen
    logger.remove(sink)


def test_cut_prompt_is_reported_with_the_fix(warnings):
    assert warn_if_truncated(model(), request(), prompt_tokens=2050)
    assert len(warnings) == 1
    text = warnings[0]
    assert "reported 2050 prompt tokens" in text and "OLLAMA_CONTEXT_LENGTH" in text and "context_window is 16384" in text


def test_whole_prompt_is_not_reported(warnings):
    # Real tokenizers count fewer tokens than the deliberately high estimate; that is not truncation.
    assert not warn_if_truncated(model(), request(), prompt_tokens=6000)
    assert warnings == []


def test_small_prompts_unknown_usage_and_anthropic_are_not_checked(warnings):
    assert not warn_if_truncated(model(), request("short"), prompt_tokens=5)
    assert not warn_if_truncated(model(), request(), prompt_tokens=0)  # server sent no usage
    claude = model(protocol="anthropic", provider="anthropic", base_url="https://api.anthropic.com")
    assert not warn_if_truncated(claude, request(), prompt_tokens=100)  # input_tokens excludes the cached prefix
    assert warnings == []


def test_other_servers_get_their_own_hint(warnings):
    assert warn_if_truncated(model(provider="vllm", context_window=None), request(), prompt_tokens=1000)
    assert "--max-model-len" in warnings[0] and "context_window is not set" in warnings[0]


def test_tool_definitions_and_calls_count_toward_the_estimate():
    base = sent_tokens(request("y" * 3500))
    tool = ToolSpec("Search_Inventory", "z" * 3500, {"type": "object"})
    assert sent_tokens(request("y" * 3500, tools=[tool])) >= base + 1000


class _Reporting(LLMProvider):
    """Answers with a fixed prompt_tokens, as a server with a 4k context does."""

    def __init__(self, settings):
        self.settings = settings

    async def complete(self, request):
        return LLMResult(content="ok", prompt_tokens=2050)

    async def stream(self, request):
        yield "ok"
        yield StreamDone(finish_reason="stop", prompt_tokens=2050)

    async def list_models(self):
        return []

    async def aclose(self):
        pass


def test_manager_checks_complete_and_stream(warnings):
    manager = ModelManager(AgentConfig(models=[model()]), _Reporting)

    async def run():
        done = await manager.complete(request(), "oss")
        assert done.result.content == "ok"  # the reply still goes out; the guard only reports
        return [item async for _, item in manager.stream(request(), "oss")]

    items = asyncio.run(run())
    assert items[0] == "ok"
    assert len(warnings) == 2

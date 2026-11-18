"""Provider-neutral types every model backend speaks."""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional, Union

from app.llm.settings import ModelConfig


@dataclass(frozen=True)
class ToolSpec:
    """A function the model may call: its name, what it does and a JSON Schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    """One call the model asked for. `arguments` is already parsed; unparsable JSON lands in `arguments_error`."""

    id: str
    name: str
    arguments: dict[str, Any]
    arguments_error: Optional[str] = None


@dataclass
class LLMRequest:
    # Provider-neutral messages, system messages included:
    #   {"role": "system" | "user" | "assistant", "content": str}
    #   {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}   the model asked for tools
    #   {"role": "tool", "tool_call_id": str, "name": str, "content": str}      a tool's result
    # An assistant turn may also carry "anthropic_blocks", the raw content Claude returned (thinking blocks must be
    # sent back unchanged while it keeps calling tools); other providers ignore it.
    messages: list[dict[str, Any]]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    reasoning_effort: Optional[str] = None  # none | low | medium | high
    max_tokens: Optional[int] = None
    stop: Optional[list[str]] = None
    response_format: Optional[dict[str, Any]] = None  # OpenAI shape
    # Leading characters of the first system message that are identical on every request (the agent prompt).
    # Providers that cache explicitly (Anthropic) mark that prefix as cacheable; OpenAI and Ollama reuse a
    # matching prefix on their own, which is why the static text always comes first.
    cache_prefix: int = 0
    tools: list[ToolSpec] = field(default_factory=list)
    # "none" keeps the tools declared (a conversation holding tool calls needs them) but makes the model answer.
    tool_choice: Optional[Literal["auto", "none"]] = None


@dataclass
class LLMResult:
    content: str
    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    anthropic_blocks: Optional[list[dict[str, Any]]] = None  # see LLMRequest.messages


@dataclass
class StreamDone:
    """Last item of a stream: finish reason and usage, when the provider reports them."""

    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


StreamItem = Union[str, StreamDone]


class LLMError(Exception):
    """A model call failed. `retryable` decides whether a fallback model is tried."""

    def __init__(self, message: str, status_code: int = 502, retryable: bool = True, code: str = "provider_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.code = code


def flatten_tool_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same conversation as plain text turns, for a model that cannot take tool calls (a fallback that has
    `supports_tools: false`): a call becomes a note on the assistant turn, a result a note on the next user turn."""
    flat: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "tool":
            flat.append({"role": "user", "content": f"[Result of {m.get('name', 'tool')}]\n{m['content']}"})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            calls = "\n".join(f"[Called {c.name}({json.dumps(c.arguments)})]" for c in m["tool_calls"])
            text = (m.get("content") or "").strip()
            flat.append({"role": "assistant", "content": f"{text}\n{calls}".strip()})
        else:
            flat.append({"role": m["role"], "content": m["content"]})
    return flat


def status_error(provider: str, status: int, message: str) -> LLMError:
    """Map an upstream HTTP status to an LLMError."""
    if status in (400, 422):
        return LLMError(f"{provider} rejected the request: {message}", 400, retryable=False, code="invalid_request")
    if status in (401, 403):
        return LLMError(f"{provider} rejected the configured api_key: {message}", 502, code="provider_auth_error")
    if status == 404:
        return LLMError(f"{provider} does not serve this model: {message}", 502, code="model_not_found")
    if status == 429:
        return LLMError(f"{provider} rate limit: {message}", 429, code="rate_limited")
    return LLMError(f"{provider} returned {status}: {message}", 502)


def timeout_error(provider: str, seconds: float) -> LLMError:
    return LLMError(f"{provider} timed out after {seconds:.0f}s", 504, code="provider_timeout")


def connection_error(provider: str, where: str) -> LLMError:
    return LLMError(f"Could not reach {provider} at {where}", 503, code="provider_unreachable")


class LLMProvider(ABC):
    def __init__(self, settings: ModelConfig) -> None:
        self.settings = settings

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResult: ...

    @abstractmethod
    def stream(self, request: LLMRequest) -> AsyncIterator[StreamItem]: ...

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Model names the server at base_url can serve right now."""

    async def aclose(self) -> None:
        return None

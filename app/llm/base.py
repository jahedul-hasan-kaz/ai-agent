"""Provider-neutral types every model backend speaks."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Union

from app.llm.settings import ModelConfig


@dataclass
class LLMRequest:
    messages: list[dict[str, str]]  # [{"role": ..., "content": ...}], system messages included
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


@dataclass
class LLMResult:
    content: str
    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


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

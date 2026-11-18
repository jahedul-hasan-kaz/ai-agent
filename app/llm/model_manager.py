"""
ModelManager: the one place that knows which models exist and how to call them.

    manager = ModelManager(agent_config)
    done = await manager.complete(LLMRequest(messages=[...]), model_id="llama-local")
    done.model.id, done.result.content

The caller always names the model; `model_type`, when given, must match it.
A failed call moves on to the model's configured `fallbacks`, unless the
request itself was rejected (bad params), which no other model would fix.
"""
import asyncio
import json
from dataclasses import dataclass, replace
from typing import AsyncIterator, Callable, Optional

from loguru import logger

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResult, StreamDone, StreamItem, flatten_tool_turns
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.llm.settings import AgentConfig, ModelConfig, ModelType
from app.services.agent_prompts import CHARS_PER_TOKEN

# The truncation guard: a server that reports far fewer prompt tokens than were sent cut the prompt to fit its
# context length (Ollama does this silently, keeping only part of the input). The estimate overcounts
# (CHARS_PER_TOKEN errs high), so only a large gap is reported, and only for prompts big enough to matter.
_TRUNCATED_BELOW = 0.5
_TRUNCATION_MIN_TOKENS = 1000


def build_provider(settings: ModelConfig) -> LLMProvider:
    adapters = {"anthropic": AnthropicProvider, "openai": OpenAICompatibleProvider}
    return adapters[settings.protocol](settings)


@dataclass
class Completion:
    model: ModelConfig  # the model that answered
    result: LLMResult
    fallback_used: bool


@dataclass
class ServerModels:
    provider: str
    protocol: str
    base_url: str
    models: list[str]
    error: Optional[str] = None


class ModelManager:
    def __init__(
        self, config: AgentConfig, provider_factory: Callable[[ModelConfig], LLMProvider] = build_provider
    ) -> None:
        self.config = config
        self._models = {m.id: m for m in config.models}
        self._providers = {m.id: provider_factory(m) for m in config.models}

    @property
    def models(self) -> list[ModelConfig]:
        return list(self._models.values())

    def resolve(self, model_id: Optional[str] = None, model_type: Optional[ModelType] = None) -> ModelConfig:
        """The model the caller asked for. There is no default: a request without
        `model` is told which ids it can choose from."""
        if not model_id:
            raise LLMError(
                f"No model specified. Set `model` to one of: {self._describe(model_type)}.",
                400,
                retryable=False,
                code="model_required",
            )

        model = self._models.get(model_id)
        if model is None:
            raise LLMError(
                f"Unknown model '{model_id}'. Available models: {self._describe(model_type)}.",
                400,
                retryable=False,
                code="unknown_model",
            )
        if model_type and model.type != model_type:
            raise LLMError(
                f"Model '{model_id}' is {model.type.value}, not {model_type.value}. "
                f"{model_type.value.capitalize()} models: {self._describe(model_type)}.",
                400,
                retryable=False,
                code="model_type_mismatch",
            )
        return model

    def _describe(self, model_type: Optional[ModelType] = None) -> str:
        """'a (frontier), b (opensource)', narrowed to one type when given."""
        matches = [m for m in self._models.values() if model_type is None or m.type == model_type]
        if not matches:
            return f"none — no {model_type.value} model is configured"
        return ", ".join(f"{m.id} ({m.type.value})" for m in matches)

    async def server_models(self) -> list[ServerModels]:
        """Ask every configured server what it serves right now — once per
        base_url, even when several config entries share it. A server that is
        down is reported with `error` instead of failing the whole list."""
        servers: dict[tuple[str, str], ModelConfig] = {}
        for m in self._models.values():
            servers.setdefault((m.protocol, m.base_url), m)

        async def query(m: ModelConfig) -> ServerModels:
            entry = ServerModels(provider=m.provider, protocol=m.protocol, base_url=m.base_url, models=[])
            try:
                entry.models = await self._providers[m.id].list_models()
            except LLMError as exc:
                entry.error = exc.message
            return entry

        return list(await asyncio.gather(*(query(m) for m in servers.values())))

    def candidates(self, model: ModelConfig) -> list[ModelConfig]:
        return [model] + [self._models[f] for f in model.fallbacks]

    async def complete(
        self, request: LLMRequest, model_id: Optional[str] = None, model_type: Optional[ModelType] = None
    ) -> Completion:
        chain = self.candidates(self.resolve(model_id, model_type))
        failures: list[tuple[str, LLMError]] = []
        for index, model in enumerate(chain):
            try:
                sent = _with_defaults(request, model)
                result = await self._providers[model.id].complete(sent)
                warn_if_truncated(model, sent, result.prompt_tokens)
                if not result.content.strip() and not result.tool_calls:
                    raise LLMError(f"{model.id} returned an empty reply", code="empty_response")
                return Completion(model=model, result=result, fallback_used=index > 0)
            except LLMError as exc:
                failures.append((model.id, exc))
                if not self._should_fall_back(exc, model, index, chain):
                    raise _combined(failures) from exc
        raise AssertionError("unreachable: the candidate chain is never empty")

    async def stream(
        self, request: LLMRequest, model_id: Optional[str] = None, model_type: Optional[ModelType] = None
    ) -> AsyncIterator[tuple[ModelConfig, StreamItem]]:
        """Yields (model, text chunk | StreamDone). Falls back only before the first chunk;
        after that the reply is half-delivered and the error is raised to the caller."""
        chain = self.candidates(self.resolve(model_id, model_type))
        failures: list[tuple[str, LLMError]] = []
        for index, model in enumerate(chain):
            emitted = False
            try:
                sent = _with_defaults(request, model)
                async for item in self._providers[model.id].stream(sent):
                    emitted = True
                    if isinstance(item, StreamDone):
                        warn_if_truncated(model, sent, item.prompt_tokens)
                    yield model, item
                return
            except LLMError as exc:
                failures.append((model.id, exc))
                if emitted or not self._should_fall_back(exc, model, index, chain):
                    raise _combined(failures) from exc

    @staticmethod
    def _should_fall_back(exc: LLMError, model: ModelConfig, index: int, chain: list[ModelConfig]) -> bool:
        has_next = index < len(chain) - 1
        logger.warning(
            "model {} failed ({}): {}{}",
            model.id,
            exc.code,
            exc.message,
            f" — falling back to {chain[index + 1].id}" if has_next and exc.retryable else "",
        )
        return exc.retryable and has_next

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()


def _combined(failures: list[tuple[str, LLMError]]) -> LLMError:
    """The error to report: the only failure as-is, or every attempt when fallbacks
    ran too, so the caller sees why the model they asked for failed."""
    if len(failures) == 1:
        return failures[0][1]
    last = failures[-1][1]
    attempts = "; ".join(f"{model_id}: {exc.message}" for model_id, exc in failures)
    return LLMError(f"All {len(failures)} models failed — {attempts}", last.status_code, last.retryable, last.code)


def sent_tokens(request: LLMRequest) -> int:
    """Rough size of what goes to the model: every message, tool call and tool definition."""
    chars = 0
    for m in request.messages:
        content = m.get("content")
        chars += len(content) if isinstance(content, str) else len(json.dumps(content, default=str)) if content else 0
        for call in m.get("tool_calls") or ():
            chars += len(call.name) + len(json.dumps(call.arguments, default=str))
    for tool in request.tools:
        chars += len(tool.name) + len(tool.description) + len(json.dumps(tool.parameters))
    return int(chars / CHARS_PER_TOKEN)


def warn_if_truncated(model: ModelConfig, request: LLMRequest, prompt_tokens: int) -> bool:
    """Log when the server reports far fewer prompt tokens than were sent: it cut the prompt, so the model
    answered without part of its instructions. Not checked for Anthropic, which rejects an oversized prompt
    instead of cutting it, and whose input_tokens leave out the cached prefix."""
    if model.protocol == "anthropic" or prompt_tokens <= 0:
        return False
    sent = sent_tokens(request)
    if sent < _TRUNCATION_MIN_TOKENS or prompt_tokens >= sent * _TRUNCATED_BELOW:
        return False
    fix = (
        "raise the server's context length (OLLAMA_CONTEXT_LENGTH, or num_ctx for this model)"
        if model.provider == "ollama"
        else "raise the server's context length (e.g. vLLM --max-model-len)"
    )
    logger.warning(
        "model {} reported {} prompt tokens for a prompt of ~{}: the server likely cut the prompt to fit its "
        "context length, so the model answered without part of its instructions. To fix, {} and set this "
        "model's context_window to match{}.",
        model.id, prompt_tokens, sent, fix,
        f" (context_window is {model.context_window})" if model.context_window else " (context_window is not set)",
    )
    return True


def _with_defaults(request: LLMRequest, model: ModelConfig) -> LLMRequest:
    """Fill what the caller left unset from this model's config entry. A model that cannot call tools gets none,
    and any tool calls already made in the conversation as plain text."""
    request = replace(
        request,
        max_tokens=request.max_tokens or model.max_tokens,
        temperature=request.temperature if request.temperature is not None else model.temperature,
    )
    if not model.supports_tools and (request.tools or any(m["role"] == "tool" for m in request.messages)):
        request = replace(request, tools=[], tool_choice=None, messages=flatten_tool_turns(request.messages))
    return request

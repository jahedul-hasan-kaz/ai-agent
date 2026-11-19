"""
ModelManager: the one place that knows which models exist and how to call them.

    manager = ModelManager(agent_config)
    done = await manager.complete(LLMRequest(messages=[...]), model_id="llama-local")
    done.model.id, done.result.content

The caller always names the model; `model_type`, when given, must match it.
A failed call moves on to the model's configured `fallbacks`, unless the
request itself was rejected (bad params), which no other model would fix.
An empty reply (no text, no tool call) is first asked again of the same model,
up to `empty_retries` more times: it is usually a one-off.
"""
import asyncio
from dataclasses import dataclass, replace
from typing import AsyncIterator, Callable, Optional

from loguru import logger

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResult, StreamDone, StreamItem, flatten_tool_turns
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.llm.settings import AgentConfig, ModelConfig, ModelType


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
        self, request: LLMRequest, model_id: Optional[str] = None, model_type: Optional[ModelType] = None,
        empty_retries: int = 0,
    ) -> Completion:
        chain = self.candidates(self.resolve(model_id, model_type))
        failures: list[tuple[str, LLMError]] = []
        for index, model in enumerate(chain):
            try:
                for attempt in range(empty_retries + 1):
                    result = await self._providers[model.id].complete(_with_defaults(request, model))
                    if result.content.strip() or result.tool_calls:
                        return Completion(model=model, result=result, fallback_used=index > 0)
                    _log_empty(model, result.finish_reason, attempt, empty_retries)
                raise _empty(model, empty_retries)
            except LLMError as exc:
                failures.append((model.id, exc))
                if not self._should_fall_back(exc, model, index, chain):
                    raise _combined(failures) from exc
        raise AssertionError("unreachable: the candidate chain is never empty")

    async def stream(
        self, request: LLMRequest, model_id: Optional[str] = None, model_type: Optional[ModelType] = None,
        empty_retries: int = 0,
    ) -> AsyncIterator[tuple[ModelConfig, StreamItem]]:
        """Yields (model, text chunk | StreamDone). Falls back only before the first chunk;
        after that the reply is half-delivered and the error is raised to the caller. A stream that ends
        without any text is an empty reply: retried like `complete`, since nothing was sent yet."""
        chain = self.candidates(self.resolve(model_id, model_type))
        failures: list[tuple[str, LLMError]] = []
        for index, model in enumerate(chain):
            emitted = False
            try:
                for attempt in range(empty_retries + 1):
                    held: list[str] = []  # whitespace before the first real text: not a reply yet
                    finish_reason = None
                    async for item in self._providers[model.id].stream(_with_defaults(request, model)):
                        if isinstance(item, StreamDone):
                            if emitted:
                                yield model, item
                            finish_reason = item.finish_reason
                            break
                        if not emitted and not item.strip():
                            held.append(item)
                            continue
                        if not emitted:
                            emitted = True
                            for chunk in held:
                                yield model, chunk
                        yield model, item
                    if emitted:
                        return
                    _log_empty(model, finish_reason, attempt, empty_retries)
                raise _empty(model, empty_retries)
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


def _log_empty(model: ModelConfig, finish_reason: Optional[str], attempt: int, retries: int) -> None:
    logger.warning(
        "model {} returned an empty reply (finish_reason={}){}", model.id, finish_reason,
        f" — retry {attempt + 1}/{retries}" if attempt < retries else "",
    )


def _empty(model: ModelConfig, retries: int) -> LLMError:
    tries = f" ({retries + 1} tries)" if retries else ""
    return LLMError(f"{model.id} returned an empty reply{tries}", code="empty_response")


def _combined(failures: list[tuple[str, LLMError]]) -> LLMError:
    """The error to report: the only failure as-is, or every attempt when fallbacks
    ran too, so the caller sees why the model they asked for failed."""
    if len(failures) == 1:
        return failures[0][1]
    last = failures[-1][1]
    attempts = "; ".join(f"{model_id}: {exc.message}" for model_id, exc in failures)
    return LLMError(f"All {len(failures)} models failed — {attempts}", last.status_code, last.retryable, last.code)


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

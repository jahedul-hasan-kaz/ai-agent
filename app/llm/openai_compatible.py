"""
Any backend that speaks the OpenAI chat completions protocol (`protocol: "openai"`).
"""
from typing import Any, AsyncIterator

import openai
from openai import AsyncOpenAI

from app.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMError,
    StreamDone,
    StreamItem,
    connection_error,
    status_error,
    timeout_error,
)
from app.llm.settings import ModelConfig


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, settings: ModelConfig) -> None:
        super().__init__(settings)
        self._client = AsyncOpenAI(
            base_url=settings.base_url,
            # Self-hosted servers ignore the key but the SDK insists on one.
            api_key=settings.api_key or "not-needed",
            timeout=settings.timeout,
            max_retries=settings.max_retries,
            default_headers=settings.headers or None,
        )

    def _params(self, request: LLMRequest) -> dict[str, Any]:
        params: dict[str, Any] = {"model": self.settings.model, "messages": request.messages}
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.top_p is not None:
            params["top_p"] = request.top_p
        if request.max_tokens is not None:
            params[self.settings.max_tokens_field] = request.max_tokens
        if request.stop:
            params["stop"] = request.stop
        if request.response_format and request.response_format.get("type") != "text":
            params["response_format"] = request.response_format
        extra_body = dict(self.settings.extra_body)
        if request.top_k is not None:
            # Not an OpenAI parameter, so it rides in the body as-is; the caller's value wins over the config's.
            extra_body["top_k"] = request.top_k
        if request.reasoning_effort:
            # The caller's choice wins over a reasoning_effort set in the model's config.
            extra_body.pop("reasoning_effort", None)
            params["reasoning_effort"] = request.reasoning_effort
        if extra_body:
            params["extra_body"] = extra_body
        return params

    def _translate(self, exc: openai.OpenAIError) -> LLMError:
        name = self.settings.provider
        if isinstance(exc, openai.APITimeoutError):
            return timeout_error(name, self.settings.timeout)
        if isinstance(exc, openai.APIConnectionError):
            return connection_error(name, self.settings.base_url or "")
        if isinstance(exc, openai.APIStatusError):
            message = exc.message
            if isinstance(exc.body, dict):
                err = exc.body.get("error", exc.body)
                if isinstance(err, dict) and err.get("message"):
                    message = str(err["message"])
            return status_error(name, exc.status_code, message)
        return LLMError(f"{name} failed: {exc}")

    async def complete(self, request: LLMRequest) -> LLMResult:
        try:
            response = await self._client.chat.completions.create(**self._params(request))
        except openai.OpenAIError as exc:
            raise self._translate(exc) from exc

        if not response.choices:
            raise LLMError(f"{self.settings.provider} returned no choices", code="empty_response")
        choice, usage = response.choices[0], response.usage
        return LLMResult(
            content=choice.message.content or "",
            finish_reason=choice.finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamItem]:
        params = self._params(request)
        if self.settings.stream_usage:
            params["stream_options"] = {"include_usage": True}

        done = StreamDone()
        try:
            stream = await self._client.chat.completions.create(stream=True, **params)
            async for chunk in stream:
                if chunk.usage:
                    done.prompt_tokens = chunk.usage.prompt_tokens
                    done.completion_tokens = chunk.usage.completion_tokens
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    yield choice.delta.content
                if choice.finish_reason:
                    done.finish_reason = choice.finish_reason
        except openai.OpenAIError as exc:
            raise self._translate(exc) from exc
        yield done

    async def list_models(self) -> list[str]:
        try:
            page = await self._client.with_options(timeout=10, max_retries=0).models.list()
        except openai.OpenAIError as exc:
            raise self._translate(exc) from exc
        return sorted(m.id for m in page.data)

    async def aclose(self) -> None:
        await self._client.close()

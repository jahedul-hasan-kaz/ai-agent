"""Models served over the Anthropic Messages API (`protocol: "anthropic"`)."""
import json
from typing import Any, AsyncIterator

import anthropic
from anthropic import AsyncAnthropic

from app.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMError,
    StreamDone,
    StreamItem,
    ToolCall,
    connection_error,
    status_error,
    timeout_error,
)
from app.llm.settings import ModelConfig

# Claude has no "none": current models always think a little, and `low` is the lightest setting.
_EFFORT = {"none": "low", "low": "low", "medium": "medium", "high": "high"}

# Anthropic stop reasons -> OpenAI finish reasons, so every provider answers alike.
_FINISH_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}


class AnthropicProvider(LLMProvider):
    def __init__(self, settings: ModelConfig) -> None:
        super().__init__(settings)
        self._client = AsyncAnthropic(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
            default_headers=settings.headers or None,
        )

    def _params(self, request: LLMRequest) -> dict[str, Any]:
        system_parts = [m["content"] for m in request.messages if m["role"] == "system"]
        if request.response_format and request.response_format.get("type") in ("json_object", "json_schema"):
            system_parts.append(_json_instruction(request.response_format))

        params: dict[str, Any] = {
            "model": self.settings.model,
            "messages": _alternating_turns(request.messages),
            "max_tokens": request.max_tokens or self.settings.max_tokens,
        }
        if system_parts:
            params["system"] = _system_blocks("\n\n".join(system_parts), request.cache_prefix)
        if request.tools:
            # Tools sit before the system prompt in Claude's cache order, so the system prefix's cache breakpoint
            # covers them too: an agent's tool list is as static as its prompt.
            params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in request.tools]
            if request.tool_choice:
                params["tool_choice"] = {"type": request.tool_choice}
        # temperature / top_p / top_k are deliberately not sent: current Claude models reject them
        # with a 400. Steer Claude with reasoning_effort instead.
        if request.stop:
            params["stop_sequences"] = request.stop
        extra_body = dict(self.settings.extra_body)
        if request.reasoning_effort:
            # Keep any other output_config the model's config sets; the caller's effort wins.
            output_config = dict(extra_body.pop("output_config", {}))
            output_config["effort"] = _EFFORT[request.reasoning_effort]
            params["output_config"] = output_config
        if extra_body:
            params["extra_body"] = extra_body
        return params

    def _translate(self, exc: anthropic.AnthropicError) -> LLMError:
        if isinstance(exc, anthropic.APITimeoutError):
            return timeout_error(self.settings.provider, self.settings.timeout)
        if isinstance(exc, anthropic.APIConnectionError):
            return connection_error(self.settings.provider, self.settings.base_url)
        if isinstance(exc, anthropic.APIStatusError):
            message = exc.message
            if isinstance(exc.body, dict) and isinstance(exc.body.get("error"), dict):
                message = exc.body["error"].get("message", message)
            return status_error(self.settings.provider, exc.status_code, message)
        return LLMError(f"{self.settings.provider} failed: {exc}")

    async def complete(self, request: LLMRequest) -> LLMResult:
        try:
            response = await self._client.messages.create(**self._params(request))
        except anthropic.AnthropicError as exc:
            raise self._translate(exc) from exc

        calls = [
            ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else {}, None if isinstance(b.input, dict) else "input is not an object")
            for b in response.content if b.type == "tool_use"
        ]
        return LLMResult(
            content="".join(block.text for block in response.content if block.type == "text"),
            finish_reason=_FINISH_REASONS.get(response.stop_reason or "", response.stop_reason),
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            tool_calls=calls,
            # Sent back verbatim on the next turn: Claude requires its own thinking blocks before a tool_result.
            anthropic_blocks=[b.model_dump(exclude_none=True) for b in response.content] if calls else None,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamItem]:
        try:
            async with self._client.messages.stream(**self._params(request)) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise self._translate(exc) from exc
        yield StreamDone(
            finish_reason=_FINISH_REASONS.get(final.stop_reason or "", final.stop_reason),
            prompt_tokens=final.usage.input_tokens,
            completion_tokens=final.usage.output_tokens,
        )

    async def list_models(self) -> list[str]:
        try:
            page = await self._client.with_options(timeout=10, max_retries=0).models.list(limit=100)
        except anthropic.AnthropicError as exc:
            raise self._translate(exc) from exc
        return sorted(m.id for m in page.data)

    async def aclose(self) -> None:
        await self._client.close()


def _system_blocks(system: str, cache_prefix: int) -> str | list[dict[str, Any]]:
    """Mark the unchanging prefix (the agent prompt) as cacheable: later requests read it from the cache
    at a fraction of the cost and latency. Prefixes under the model's minimum are simply not cached."""
    if cache_prefix <= 0 or cache_prefix > len(system):
        return system
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system[:cache_prefix], "cache_control": {"type": "ephemeral"}}
    ]
    if rest := system[cache_prefix:].strip():
        blocks.append({"type": "text", "text": rest})
    return blocks


def _alternating_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude needs user/assistant turns that alternate and open with the user:
    merge same-role runs and prefix a stub user turn when the assistant spoke first.
    Tool calls become `tool_use` blocks on the assistant turn, their results `tool_result` blocks on the next user turn."""
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "system":
            continue
        role, content = _turn(message)
        if turns and turns[-1]["role"] == role:
            previous = turns[-1]["content"]
            if isinstance(previous, str) and isinstance(content, str):
                merged: Any = f"{previous}\n\n{content}"
            else:
                merged = _blocks(previous) + _blocks(content)
                # tool_result blocks must open the user turn that answers a tool_use.
                merged.sort(key=lambda b: b.get("type") != "tool_result")
            turns[-1] = {"role": role, "content": merged}
        else:
            turns.append({"role": role, "content": content})
    if turns and turns[0]["role"] == "assistant":
        turns.insert(0, {"role": "user", "content": "(The dealership started this conversation.)"})
    return turns


def _turn(message: dict[str, Any]) -> tuple[str, Any]:
    if message["role"] == "tool":
        return "user", [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}]
    if message["role"] == "assistant" and message.get("tool_calls"):
        if message.get("anthropic_blocks"):
            return "assistant", list(message["anthropic_blocks"])
        blocks: list[dict[str, Any]] = [{"type": "text", "text": message["content"]}] if (message.get("content") or "").strip() else []
        blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in message["tool_calls"]]
        return "assistant", blocks
    return message["role"], message["content"]


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _json_instruction(response_format: dict[str, Any]) -> str:
    schema = (response_format.get("json_schema") or {}).get("schema")
    if schema:
        return (
            "Respond with a single JSON object that validates against this JSON Schema, "
            f"and nothing else (no prose, no code fences):\n{json.dumps(schema)}"
        )
    return "Respond with a single valid JSON object and nothing else (no prose, no code fences)."

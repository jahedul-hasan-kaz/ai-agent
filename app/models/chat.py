"""
Chat completion API schemas — the OpenAI chat completion contract, so any
OpenAI-compatible client can call this service, plus `model_type`.
"""
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.llm.settings import ModelType


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1, max_length=100_000)


class JsonSchemaFormat(BaseModel):
    name: str = "response"
    schema_: dict[str, Any] = Field(..., alias="schema")
    strict: bool = True

    model_config = {"populate_by_name": True}


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: Optional[JsonSchemaFormat] = None


class ChatCompletionRequest(BaseModel):
    # Optional in the schema so a missing model gets a 400 listing the choices, not a bare 422.
    model: Optional[str] = Field(None, description="Required. A model id from ai-agent-config (see GET /v1/models).")
    model_type: Optional[ModelType] = Field(None, description="frontier or opensource; must match `model` if given.")
    messages: list[ChatMessage] = Field(..., min_length=1)
    agent: Optional[str] = Field(
        None,
        max_length=100,
        description="Which agent answers: its system prompt is app/prompts/agents/<agent>_system_instr.md "
        "(see GET /v1/agents). Default: general.",
    )
    tenant_id: Optional[UUID] = Field(
        None,
        description="The dealership (docs.tenant_id) whose knowledge base grounds the reply. "
        "Without it nothing is retrieved and the model is told it has no dealership facts.",
    )
    sms_thread_key: Optional[str] = Field(
        None,
        max_length=255,
        description="The customer's SMS number (sms_log.thread_key, e.g. +15551234567). With tenant_id, the SMS "
        "history and the customer's leads and vehicles are loaded into the prompt.",
    )
    email_thread_key: Optional[str] = Field(
        None,
        max_length=255,
        description="The customer's email address. With tenant_id, the email history and the customer's leads and "
        "vehicles are loaded into the prompt.",
    )
    store: bool = Field(
        False,
        description="Save this exchange in the backend's message log: the latest user message (inbound, `received`) "
        "and the reply (outbound, `delivered`), in sms_log or email_log. Needs exactly one thread key.",
    )
    sent_by: Optional[str] = Field(None, max_length=255, description="sent_by of the stored reply (default `agent`).")
    cnp_provider: Optional[str] = Field(
        None, max_length=50, description="Provider of the stored rows (sms_log.cnp_provider / email_log.provider); "
        "default: carried over from the thread's latest message.",
    )
    step_num: Optional[int] = Field(
        None, ge=0, description="Only with `store`: step_num of the stored rows (default: carried over from the thread's latest message)."
    )
    use_tools: bool = Field(
        True,
        description="Let the agent call its tools (the ones wired to it in the n8n SMS handler, see GET /v1/agents). "
        "false: the agent answers from its prompt alone.",
    )
    temperature: Optional[float] = Field(None, ge=0, le=2)
    top_p: Optional[float] = Field(None, gt=0, le=1)
    top_k: Optional[int] = Field(
        None,
        ge=1,
        le=1000,
        description="Sample only from the k most likely tokens. Not part of OpenAI's own API: sent to OpenAI-compatible "
        "servers that accept it (Ollama, vLLM, OpenRouter, ...).",
    )
    reasoning_effort: Optional[Literal["none", "low", "medium", "high"]] = Field(
        None,
        description="How much the model thinks before answering. 'none' is fastest; 'high' is slowest and most "
        "thorough. Unset uses the model's own default (or its extra_body setting).",
    )
    max_tokens: Optional[int] = Field(None, ge=1, le=32_000)
    stop: Optional[list[str]] = Field(None, max_length=4)
    response_format: Optional[ResponseFormat] = None
    stream: bool = False

    @field_validator("messages")
    @classmethod
    def _needs_a_user_turn(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if not any(m.role == "user" for m in messages):
            raise ValueError("messages must contain at least one 'user' message")
        return messages

    @field_validator("sms_thread_key", "email_thread_key")
    @classmethod
    def _blank_is_none(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() or None if value else None

    @model_validator(mode="after")
    def _thread_keys_need_a_tenant(self) -> "ChatCompletionRequest":
        # Customer records are per dealership; looking a number up across tenants would leak another dealer's data.
        if (self.sms_thread_key or self.email_thread_key) and self.tenant_id is None:
            raise ValueError("tenant_id is required with sms_thread_key or email_thread_key")
        if self.store and bool(self.sms_thread_key) == bool(self.email_thread_key):
            raise ValueError("store needs exactly one of sms_thread_key or email_thread_key")
        return self


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def of(cls, prompt_tokens: int, completion_tokens: int) -> "Usage":
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class StoredMessages(BaseModel):
    """Rows written when the request set `store`. On failure only `error` is set; the reply is still returned."""

    channel: Optional[Literal["sms", "email"]] = None
    inbound_id: Optional[str] = None
    outbound_id: Optional[str] = None
    error: Optional[str] = None


class ToolCallInfo(BaseModel):
    """One tool call made while answering, in the order made."""

    id: str
    name: str
    kind: str = Field(..., description="The n8n node it replaces: db (Supabase), code (Code tool) or http (HTTP Request tool).")
    arguments: dict[str, Any]
    ok: bool = Field(..., description="false when the tool failed, timed out or refused (see `error`).")
    latency_ms: int
    error: Optional[str] = None
    dry_run: bool = Field(False, description="The tool changes data but tools.write_mode is dry_run: nothing was written.")


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    model_type: ModelType
    choices: list[Choice]
    usage: Usage
    latency_ms: int
    fallback_used: bool = False
    tools_used: list[str] = Field(
        default_factory=list, description="Names of the tools called for this completion, each once, in first-use order."
    )
    tool_calls: list[ToolCallInfo] = Field(default_factory=list, description="Every tool call, with its arguments and outcome.")
    stored: Optional[StoredMessages] = None


class ModelInfo(BaseModel):
    id: str
    type: ModelType
    provider: str
    model: str


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


class AgentInfo(BaseModel):
    id: str
    label: str
    default: bool = False
    prompt_tokens: int = Field(..., description="Estimated size of the agent's instructions.")
    tools: list[str] = Field(default_factory=list, description="Tools this agent can call (when the request allows them).")


class AgentListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[AgentInfo]


class ServerModelsInfo(BaseModel):
    provider: str
    protocol: str
    base_url: str
    models: list[str]
    error: Optional[str] = None


class ServerModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ServerModelsInfo]

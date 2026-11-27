"""
Chat completion API schemas — the OpenAI chat completion contract, so any
OpenAI-compatible client can call this service, plus `model_type`.
"""
import json
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.llm.settings import ModelType
from app.services.node_data import INTERNAL_NODES

_MAX_NODE_DATA_CHARS = 200_000


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
        description="The dealership (tenants.id): its records, the customer's lead and the tools are looked up "
        "with it. The knowledge base is static and sent without it.",
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
    node_data: Optional[dict[str, Any]] = Field(
        None,
        description="Outputs of the n8n SMS handler's computed upstream nodes that the agent's prompt and tools read, "
        "keyed by node name, each shaped like that node's `$('<node>').first().json`. E.g. {\"Intent Classifier\": "
        "{\"intent\": \"inventory\"}}. A supplied value wins; anything left out is derived as usual. Database-backed "
        "nodes (the tenant, lead, vehicle, SMS history, ...) and the clock (Get Current Date/Time) are not accepted: "
        "the gateway reads or computes them itself. GET /v1/agents lists the nodes and fields each agent reads.",
    )
    record_opt_out: bool = Field(
        True,
        description="Record opt-outs and DNCs in the database. false: an opt-out is still detected and answered the "
        "same way, but nothing is written - no public.process_opt_out call, no DNC row from Suppress_Lead (its "
        "cadence halt and send hold still apply), and with `store` the customer's opt-out message is not logged "
        "(sms_log's own opt-out trigger would record a DNC). For testing; keep true in production.",
    )
    opt_out_gate: bool = Field(
        True,
        description="SMS threads: before any agent runs, an opt-out message (STOP, unsubscribe, 'stop texting me', "
        "'wrong number', ... - the n8n Opt-out Detector's rules) is recorded with public.process_opt_out (write_mode "
        "applies) and answered with the fixed confirmation, and a customer who already opted out gets no reply "
        "(<NOOP>). false: skip this check.",
    )
    reroute_noop: bool = Field(
        True,
        description="When the agent answers <NOOP> to a real message (it belongs to another agent), run the turn once "
        "more on the agent its wording points to, as the n8n workflow does; if that agent declines too, send the "
        "workflow's safe fallback and page a person. A <NOOP> to a closing ('thanks', 'ok bye') stays <NOOP>. "
        "false: return the agent's <NOOP> as is.",
    )
    empty_retries: int = Field(
        3,
        ge=0,
        le=10,
        description="How many more times to ask the same model when it returns an empty reply (no text and no "
        "tool call), before falling back to the next model or failing with empty_response. 0: no retry.",
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

    @field_validator("node_data")
    @classmethod
    def _node_data_shape(cls, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not value:
            return None
        internal = sorted(set(value) & INTERNAL_NODES)
        if internal:
            raise ValueError(
                f"node_data cannot supply {', '.join(internal)}: the gateway reads or computes it itself (database "
                "nodes from tenant_id and sms_thread_key / email_thread_key, the clock from the server)"
            )
        for name, output in value.items():
            if not isinstance(output, (dict, list)):
                raise ValueError(f"node_data['{name}'] must be the node's JSON object (or a list of rows)")
        if len(json.dumps(value, default=str)) > _MAX_NODE_DATA_CHARS:
            raise ValueError(f"node_data is larger than {_MAX_NODE_DATA_CHARS} characters")
        return value

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
    from_text: bool = Field(
        False, description="The model wrote this call into its reply as JSON instead of calling the tool; the gateway "
        "ran it as a real call and kept the JSON out of the reply.",
    )


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: Optional[str] = None


class RoutingInfo(BaseModel):
    """What happened when the agent answered <NOOP> (see `reroute_noop`)."""

    outcome: Literal[
        "silent_closing", "silent_suppressed", "silent_escalated", "rerouted", "fallback", "silent_confirmed",
        "opted_out", "silent_opted_out", "empty_fallback",
    ] = Field(
        ..., description="silent_closing: the customer only closed the conversation, nothing to send. silent_suppressed / "
        "silent_escalated: a tool already handled the turn. rerouted: another agent answered. fallback: that agent "
        "declined too; the safe fallback was sent and a person paged. silent_confirmed: general declined twice; "
        "nothing sent, a person paged. opted_out: the message was an opt-out (n8n Opt-out Detector); it was "
        "recorded (process_opt_out) and the fixed confirmation sent, no agent ran. silent_opted_out: the customer had "
        "already opted out; no agent ran and nothing is sent. empty_fallback: the model kept returning an empty reply; "
        "the safe fallback was sent and a person paged (instead of an empty_response error).",
    )
    from_agent: str = Field(..., description="The agent that answered <NOOP>.")
    to_agent: Optional[str] = Field(None, description="The agent the turn was run on next.")


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
    routing: Optional[RoutingInfo] = Field(None, description="Set when the agent answered <NOOP>; see `reroute_noop`.")


class ModelInfo(BaseModel):
    id: str
    type: ModelType
    provider: str
    model: str


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


class NodeInput(BaseModel):
    """One upstream n8n node whose output this agent reads, and can be supplied in `node_data`."""

    node: str
    fields: list[str] = Field(default_factory=list, description="Fields read from it (dotted paths). Empty: the whole output.")
    list_of_rows: bool = Field(False, description="The node's output is a list of rows, not one object.")
    used_by: list[str] = Field(default_factory=list, description="'prompt' and/or the tool names that read it.")


class AgentInfo(BaseModel):
    id: str
    label: str
    default: bool = False
    prompt_tokens: int = Field(..., description="Estimated size of the agent's instructions.")
    tools: list[str] = Field(default_factory=list, description="Tools this agent can call (when the request allows them).")
    inputs: list[NodeInput] = Field(
        default_factory=list, description="Upstream workflow nodes the agent's prompt and tools read (see node_data)."
    )


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

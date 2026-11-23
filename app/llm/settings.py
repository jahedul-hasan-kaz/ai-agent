"""
Schema of the `ai-agent-config` JSON: every model the gateway can serve.

{
  "service_api_key": "callers send this as a Bearer token (empty = no auth, dev only)",
  "models": [
    {"id": "claude-sonnet", "type": "frontier", "provider": "anthropic", "protocol": "anthropic",
     "model": "claude-sonnet-5-5", "base_url": "https://api.anthropic.com", "api_key": "sk-ant-...",
     "fallbacks": ["llama-local"]},
    {"id": "llama-local", "type": "opensource", "provider": "ollama", "protocol": "openai",
     "model": "llama3.2:1b", "base_url": "http://localhost:11434/v1"}
  ]
}

Nothing about a vendor is hardcoded: `protocol` picks the wire format and every
vendor difference is a field on the model entry.
"""
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ModelType(str, Enum):
    frontier = "frontier"
    opensource = "opensource"


# Wire protocols the gateway speaks. "openai" covers every OpenAI-compatible
# server (OpenAI, OpenRouter, Gemini, Groq, Ollama, vLLM, LM Studio, ...).
Protocol = Literal["anthropic", "openai"]


class ModelConfig(BaseModel):
    id: str = Field(..., min_length=1, description="Alias callers send as `model`.")
    type: ModelType
    provider: str = Field(..., min_length=1, description="Free-text vendor label for logs and errors, e.g. 'ollama'.")
    protocol: Protocol
    model: str = Field(..., min_length=1, description="Provider-native model name.")
    api_key: str = Field("", description="Leave empty for servers that need no key (e.g. local Ollama).")
    base_url: str = Field(..., description="Provider endpoint, e.g. https://api.openai.com/v1. Always set explicitly.")
    headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(
        default_factory=dict,
        description='Extra request fields sent as-is, e.g. {"reasoning_effort": "none"} to stop a model thinking.',
    )

    # Defaults applied when the request leaves them unset.
    max_tokens: int = Field(1000, ge=1)
    context_window: Optional[int] = Field(
        None,
        ge=1024,
        description="Tokens the model accepts (prompt + reply). When set, every prompt is fitted inside it: the "
        "agent's instructions are always sent whole, and history and retrieved context are trimmed to fit. "
        "For Ollama this must match the server's context length (OLLAMA_CONTEXT_LENGTH or the model's num_ctx), "
        "which defaults far below the agent prompts' size and otherwise silently cuts the prompt's start.",
    )
    temperature: Optional[float] = Field(0.8, ge=0, le=2)
    timeout: float = Field(60, gt=0, description="Seconds per attempt.")
    max_retries: int = Field(2, ge=0, le=5, description="SDK retries on transient errors.")

    # OpenAI-protocol quirks; the defaults suit most servers.
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = Field(
        "max_tokens", description="OpenAI's own API wants max_completion_tokens."
    )
    stream_usage: bool = Field(True, description="Ask for token usage when streaming; false for servers that reject it (Gemini).")

    supports_tools: bool = Field(
        True,
        description="The model can call the agent's tools (function calling). Set false for servers or small local "
        "models that reject `tools`; that model then answers from the prompt alone.",
    )
    tool_reasoning_effort: Optional[Literal["low", "medium", "high"]] = Field(
        None,
        description="The least reasoning_effort used when the request offers tools: a request (or extra_body) asking "
        "for 'none' gets this instead. For models that write tool calls as plain text when not reasoning (gpt-oss: "
        "'low'). Unset: no change.",
    )

    fallbacks: list[str] = Field(default_factory=list, description="Model ids tried, in order, if this one fails.")

    @model_validator(mode="after")
    def _check_connection(self) -> "ModelConfig":
        self.base_url = self.base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"model '{self.id}': base_url must start with http:// or https://")
        return self


class EmbeddingConfig(BaseModel):
    """OpenAI-compatible /embeddings endpoint. Must be the same model (and dimensions) that wrote `docs.embedding`."""

    provider: str = Field(..., min_length=1, description="Free-text vendor label for logs, e.g. 'openai'.")
    model: str = Field(..., min_length=1, description="Embedding model name, e.g. text-embedding-3-small.")
    base_url: str = Field(..., description="e.g. https://api.openai.com/v1")
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    dimensions: Optional[int] = Field(
        None, ge=1, description="Sent to the API only when set (text-embedding-3 can shorten vectors)."
    )
    timeout: float = Field(10, gt=0)
    max_retries: int = Field(1, ge=0, le=5)

    @model_validator(mode="after")
    def _check_connection(self) -> "EmbeddingConfig":
        self.base_url = self.base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("rag.embedding: base_url must start with http:// or https://")
        return self


class RagConfig(BaseModel):
    """Retrieval over the backend's `docs` table (pgvector, cosine). Retrieval only: nothing is written."""

    enabled: bool = True
    embedding: EmbeddingConfig
    top_k: int = Field(5, ge=1, le=20, description="Most docs put into one prompt.")
    min_similarity: float = Field(
        0.3, ge=-1, le=1, description="Cosine similarity floor; weaker matches are dropped. Tune per embedding model."
    )
    max_similarity_gap: float = Field(
        0.15, ge=0, le=2, description="Drop docs scoring this far below the best match (keeps only the relevant cluster)."
    )
    max_context_tokens: int = Field(1500, ge=100, le=20_000, description="Budget for all retrieved text (~4 chars/token).")
    max_doc_tokens: int = Field(600, ge=50, le=20_000, description="One doc is cut to this before it enters the budget.")
    kinds: Optional[list[str]] = Field(None, description="Only these docs.kind values; null = every kind.")
    timeout: float = Field(5, gt=0, description="Seconds for embed + search; slower answers go out without context.")


class CustomerConfig(BaseModel):
    """The customer's own records (sms_log, email_log, leads, lead_vehicles, tenants), read when a request
    carries sms_thread_key / email_thread_key. Read only."""

    enabled: bool = True
    history_tokens: int = Field(
        4000, ge=200, le=50_000, description="Budget for the conversation history across SMS and email (~4 chars/token)."
    )
    recent_messages: int = Field(40, ge=1, le=500, description="Newest messages per channel always considered verbatim.")
    relevant_messages: int = Field(
        8, ge=0, le=50, description="Older messages per channel brought back because they match the customer's latest message."
    )
    older_messages: int = Field(
        300, ge=0, le=5000, description="Older non-campaign messages per channel read to fill the rest of the budget."
    )
    max_message_chars: int = Field(1500, ge=100, le=20_000, description="One message is cut to this before it enters the budget.")
    max_vehicles: int = Field(30, ge=1, le=200)
    timeout: float = Field(5, gt=0, description="Seconds for every customer query; slower answers go out without it.")


class ToolsConfig(BaseModel):
    """The tools each agent can call (the ones wired to it in the n8n SMS handler), see app/tools."""

    enabled: bool = True
    write_mode: Literal["dry_run", "live"] = Field(
        "dry_run",
        description="dry_run: tools that change data (Save_Appointment_Notes, Update_mileage, Escalate_To_Human, "
        "Suppress_Lead, Cancel_Appointment) run every check and read, then report what they would have written "
        "(`dry_run: true`) without writing. live: they write to the backend's Postgres, as the n8n workflow does "
        "(an escalation row pages the dealer).",
    )
    max_iterations: int = Field(6, ge=1, le=20, description="Most model turns that call tools before it must answer.")
    timeout: float = Field(15, gt=0, description="Seconds for one tool call; a slower call returns an error to the model.")
    max_result_chars: int = Field(8000, ge=500, le=100_000, description="A tool result is cut to this before the model sees it.")
    disabled: list[str] = Field(default_factory=list, description="Tool names never offered, e.g. [\"Search_Inventory\"].")
    marketcheck_api_key: str = Field("", description="Search_Inventory's MarketCheck key. Empty: Search_Inventory is not offered.")
    marketcheck_base_url: str = "https://api.marketcheck.com"
    knowledge_base_limit: int = Field(3, ge=1, le=10, description="Docs Knowledge_Base_Search returns (n8n: 3).")


class PlaygroundConfig(BaseModel):
    """The web UI's chat tester (/v1/playground). It has no auth of its own, so it stays off unless enabled."""

    enabled: bool = False
    cors_origins: list[str] = Field(default_factory=list, description="Web UI origins allowed to call the API.")


class AgentConfig(BaseModel):
    service_api_key: str = ""
    models: list[ModelConfig] = Field(..., min_length=1)
    rag: Optional[RagConfig] = None
    customer: CustomerConfig = Field(default_factory=CustomerConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    playground: PlaygroundConfig = Field(default_factory=PlaygroundConfig)

    @model_validator(mode="after")
    def _check_references(self) -> "AgentConfig":
        by_id = {}
        for m in self.models:
            if m.id in by_id:
                raise ValueError(f"duplicate model id '{m.id}'")
            by_id[m.id] = m

        for m in self.models:
            unknown = [f for f in m.fallbacks if f not in by_id or f == m.id]
            if unknown:
                raise ValueError(f"model '{m.id}' has invalid fallbacks: {unknown}")
        return self

"""
Which tools each agent has, and running them.

The wiring is the n8n SMS handler's (workflow 328CXNQvb2QWXXWk, its `ai_tool` connections): an agent is offered only
its own tools. Book_Service_Appointment and Get_Service_Pricing are disabled in that workflow and wired to no
agent, so they are not here, even though some prompts still mention them.

A tool is offered only when this request can run it (see Tool.needs): e.g. without `sms_thread_key` there is no
lead to escalate for, so Escalate_To_Human is left out instead of failing mid-conversation. A tool that fails or
times out returns `{"ok": false, "error": ...}` to the model; it never fails the chat completion.
"""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg
import httpx
from loguru import logger

from app.llm.base import ToolCall, ToolSpec
from app.llm.settings import ToolsConfig
from app.rag.retriever import Retriever
from app.tools.appointments import CANCEL_APPOINTMENT, SAVE_APPOINTMENT_NOTES, VALIDATE_APPOINTMENT
from app.tools.base import (
    NEEDS_CUSTOMER,
    NEEDS_DB,
    NEEDS_MARKETCHECK,
    NEEDS_RETRIEVER,
    NEEDS_SMS,
    NEEDS_TENANT,
    Tool,
    ToolContext,
    ToolFailure,
)
from app.tools.escalation import ESCALATE_TO_HUMAN, SUPPRESS_LEAD
from app.services.node_data import INTERNAL_NODES
from app.tools.inventory import SEARCH_INVENTORY
from app.tools.records import GET_SMS_CONVERSATION_LOG, KNOWLEDGE_BASE_SEARCH, UPDATE_MILEAGE

ALL_TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        KNOWLEDGE_BASE_SEARCH, GET_SMS_CONVERSATION_LOG, UPDATE_MILEAGE, VALIDATE_APPOINTMENT, SAVE_APPOINTMENT_NOTES,
        CANCEL_APPOINTMENT, SEARCH_INVENTORY, ESCALATE_TO_HUMAN, SUPPRESS_LEAD,
    )
}

AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "general": ("Escalate_To_Human", "Get_SMS_conversation_log", "Knowledge_Base_Search", "Suppress_Lead"),
    "inventory": ("Escalate_To_Human", "Search_Inventory"),
    "scheduling": ("Cancel_Appointment", "Escalate_To_Human", "Save_Appointment_Notes", "Validate_Appointment"),
    "value_data_collection": ("Escalate_To_Human", "Knowledge_Base_Search", "Save_Appointment_Notes", "Update_mileage"),
    "service_trade_reply": ("Escalate_To_Human", "Get_SMS_conversation_log", "Knowledge_Base_Search", "Suppress_Lead"),
}

_RECONNECT_EVERY = 30.0
_MAX_ARGUMENT_CHARS = 2000


@dataclass
class ToolRun:
    """One executed call: what the API reports back in `tool_calls`."""

    id: str
    name: str
    kind: str
    arguments: dict[str, Any]
    ok: bool
    latency_ms: int
    error: Optional[str] = None
    dry_run: bool = False


def agent_tools(agent_id: str) -> list[Tool]:
    return [ALL_TOOLS[name] for name in AGENT_TOOLS.get(agent_id, ())]


def agent_inputs(agent_id: str, prompt_inputs: dict[str, list[str]]) -> list[dict[str, Any]]:
    """The computed workflow nodes an agent reads, for node_data: its prompt's expressions, then its tools' Tool
    Context fields. Database-backed nodes and the clock are left out: the gateway reads or computes those itself.
    [{node, fields, list_of_rows, used_by}]."""
    out: dict[str, dict[str, Any]] = {}

    def add(node: str, path: str, user: str) -> None:
        if node in INTERNAL_NODES:
            return
        entry = out.setdefault(node, {"node": node, "fields": [], "list_of_rows": False, "used_by": []})
        if path and path not in entry["fields"]:
            entry["fields"].append(path)
        if user not in entry["used_by"]:
            entry["used_by"].append(user)

    for node, fields in prompt_inputs.items():
        for path in fields or [""]:
            add(node, path, "prompt")
    for tool in agent_tools(agent_id):
        for node, path in tool.inputs:
            add(node, path, tool.name)
    return list(out.values())


def spec(tool: Tool) -> ToolSpec:
    return ToolSpec(tool.name, tool.description, tool.parameters)


class ToolRunner:
    def __init__(self, settings: ToolsConfig, dsn: str = "", retriever: Optional[Retriever] = None) -> None:
        self.settings = settings
        self.retriever = retriever
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=settings.timeout)

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def start(self) -> None:
        """Never raises: without the database, tools that need it are simply not offered."""
        if not self._dsn:
            logger.warning("tools: PG_DB_URL is empty — tools that read or write the backend's records are off")
            return
        self._last_attempt = time.monotonic()
        try:
            # statement_cache_size=0: PG_DB_URL is Supavisor's transaction pooler.
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=1, max_size=5, statement_cache_size=0, command_timeout=self.settings.timeout,
                timeout=min(self.settings.timeout, 10),  # a dead database must not hold up a chat request
            )
            logger.info("tools ready (write_mode={})", self.settings.write_mode)
        except Exception as exc:  # noqa: BLE001
            logger.error("tools: database unavailable: {}", f"{type(exc).__name__}: {exc}"[:300])

    async def _reconnect(self) -> None:
        if self._pool is not None or not self._dsn or time.monotonic() - self._last_attempt < _RECONNECT_EVERY:
            return
        async with self._lock:
            if self._pool is None and time.monotonic() - self._last_attempt >= _RECONNECT_EVERY:
                await self.start()

    async def aclose(self) -> None:
        await self.http.aclose()
        if self._pool is not None:
            await self._pool.close()

    async def context(
        self, tenant_id, sms_thread_key, email_thread_key, turns: list[dict[str, Any]],
        node_data: Optional[dict[str, Any]] = None,
    ) -> ToolContext:
        await self._reconnect()
        return ToolContext(
            settings=self.settings, tenant_id=tenant_id, sms_thread_key=sms_thread_key, email_thread_key=email_thread_key,
            turns=turns, pool=self._pool, http=self.http, retriever=self.retriever, node_data=node_data or {},
        )

    def offered(self, agent_id: str, ctx: ToolContext) -> tuple[list[Tool], dict[str, str]]:
        """The agent's tools this request can run, and why the others are left out."""
        offered, skipped = [], {}
        for tool in agent_tools(agent_id):
            reason = "disabled in tools.disabled" if tool.name in self.settings.disabled else self._missing(tool, ctx)
            if reason:
                skipped[tool.name] = reason
            else:
                offered.append(tool)
        return offered, skipped

    def _missing(self, tool: Tool, ctx: ToolContext) -> Optional[str]:
        for need in tool.needs:
            if need == NEEDS_TENANT and ctx.tenant_id is None:
                return "no tenant_id on the request"
            if need == NEEDS_CUSTOMER and (ctx.tenant_id is None or not (ctx.sms_thread_key or ctx.email_thread_key)):
                return "no tenant_id with sms_thread_key / email_thread_key, so no lead to act on"
            if need == NEEDS_SMS and (ctx.tenant_id is None or not ctx.sms_thread_key):
                return "no tenant_id with sms_thread_key"
            if need == NEEDS_DB and ctx.pool is None:
                return "the backend's database is not reachable"
            if need == NEEDS_RETRIEVER and ctx.retriever is None:
                return "rag (the docs knowledge base) is not configured"
            if need == NEEDS_MARKETCHECK and not self.settings.marketcheck_api_key:
                return "tools.marketcheck_api_key is not set"
        return None

    async def run(self, tools: dict[str, Tool], call: ToolCall, ctx: ToolContext) -> tuple[str, ToolRun]:
        """Execute one call. Returns the text the model gets back and the record the API reports."""
        started = time.perf_counter()
        tool = tools.get(call.name)
        error: Optional[str] = None
        result: dict[str, Any]
        if tool is None:
            error = f"unknown tool {call.name!r}; available: {', '.join(tools)}"
            result = {"ok": False, "error": error}
        elif call.arguments_error:
            error = call.arguments_error
            result = {"ok": False, "error": f"{call.arguments_error}. Call {call.name} again with a JSON object."}
        else:
            try:
                result = await asyncio.wait_for(tool.run(ctx, call.arguments), self.settings.timeout)
            except asyncio.TimeoutError:
                error = f"timed out after {self.settings.timeout:g}s"
                result = {"ok": False, "error": f"{call.name} {error}; nothing can be assumed to have been done."}
            except ToolFailure as exc:
                error = str(exc)
                result = {"ok": False, "error": error}
            except Exception as exc:  # noqa: BLE001 - a tool bug must not fail the reply
                error = f"{type(exc).__name__}: {exc}"[:300]
                logger.exception("tool {} failed", call.name)
                result = {"ok": False, "error": f"{call.name} failed; nothing can be assumed to have been done."}
        if error is None:
            error = _reported_error(result)
        content = json.dumps(result, default=str, ensure_ascii=False)
        if len(content) > self.settings.max_result_chars:
            content = content[: self.settings.max_result_chars] + " …[truncated]"
        record = ToolRun(
            id=call.id, name=call.name, kind=tool.kind if tool else "unknown", arguments=_short(call.arguments),
            ok=error is None, latency_ms=int((time.perf_counter() - started) * 1000), error=error,
            dry_run=bool(result.get("dry_run")) if isinstance(result, dict) else False,
        )
        logger.info(
            "tool {} ok={} ms={}{}{}", call.name, record.ok, record.latency_ms, " dry_run" if record.dry_run else "",
            f" error={error}" if error else "",
        )
        return content, record


def _reported_error(result: Any) -> Optional[str]:
    """A tool that ran but refused or could not act reports it in its result."""
    if not isinstance(result, dict):
        return None
    status = result.get("statusCode")
    if isinstance(status, int) and status >= 400:
        body = result.get("body")
        return str(body.get("message") if isinstance(body, dict) else body)[:300]
    if result.get("ok") is False or "error" in result:
        return str(result.get("error") or result.get("blocked") or "not ok")[:300]
    return None


def _short(arguments: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(arguments, default=str)
    return arguments if len(text) <= _MAX_ARGUMENT_CHARS else {"_truncated": text[:_MAX_ARGUMENT_CHARS]}

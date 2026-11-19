"""What a tool is, and the per-request context every tool runs against."""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import httpx
from loguru import logger

from app.customer.store import email_variants, phone_variants
from app.llm.settings import ToolsConfig
from app.rag.retriever import Retriever
from app.services import node_data as nd

# The n8n node each tool replaces: a Supabase node (db), a Code tool (code) or an HTTP Request tool (http).
ToolKind = Literal["db", "code", "http"]

DEFAULT_TIMEZONE = "America/New_York"

# Requirements a tool can declare. A tool whose requirements this request cannot meet is not offered to the model.
NEEDS_TENANT = "tenant"  # tenant_id on the request
NEEDS_CUSTOMER = "customer"  # tenant_id plus a thread key or a supplied "Lookup Lead": there is a lead to act on
NEEDS_SMS = "sms"  # tenant_id plus sms_thread_key
NEEDS_DB = "db"  # the backend's Postgres is reachable
NEEDS_RETRIEVER = "retriever"  # the docs knowledge base (rag) is configured
NEEDS_MARKETCHECK = "marketcheck"  # tools.marketcheck_api_key is set


class ToolFailure(Exception):
    """A tool could not do its job. Its message goes back to the model as `{"ok": false, "error": ...}`."""


@dataclass(frozen=True)
class Tool:
    name: str
    kind: ToolKind
    description: str
    parameters: dict[str, Any]
    run: Callable[["ToolContext", dict[str, Any]], Awaitable[dict[str, Any]]]
    needs: tuple[str, ...] = ()
    writes: bool = False  # changes data; in write_mode=dry_run it reports what it would write instead
    # (node, field) the n8n tool reads from upstream workflow nodes (via its Tool Context): what a caller can
    # supply in node_data. field '' means the node's whole output.
    inputs: tuple[tuple[str, str], ...] = ()


def schema(properties: Optional[dict[str, Any]] = None, required: Optional[list[str]] = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


_LEAD_SQL = """
SELECT id, tenant_id, phone, first_name, last_name, email, current_cadence_id
FROM public.leads
WHERE tenant_id = $1
  AND (phone = ANY($2::text[]) OR btrim(thread_id) = ANY($2::text[]) OR lower(email) = ANY($3::text[]))
ORDER BY created_at DESC
LIMIT 1
"""

_TENANT_SQL = "SELECT id, slug, hours_of_operation, main_phone FROM public.tenants WHERE id = $1"

# The vehicle the conversation is about: the backend's own resolver first (it weighs what the customer just said),
# the lead's primary vehicle when that function is not there.
_RESOLVE_VEHICLE_SQL = "SELECT v FROM public.resolve_thread_vehicle($1, $2, $3) AS v LIMIT 1"
_PRIMARY_VEHICLE_SQL = """
SELECT id, lead_id, year, make, model, trim, vin, mileage, mileage_update_date
FROM public.lead_vehicles WHERE lead_id = $1
ORDER BY is_primary DESC NULLS LAST, created_at DESC
LIMIT 1
"""

# n8n "Get Active Cadence": the campaign of the lead's latest campaign message.
_ACTIVE_CADENCE_SQL = """
SELECT cadence_id, step_num FROM public.sms_log
WHERE lead_id = $1 AND tenant_id = $2 AND cadence_id IS NOT NULL
ORDER BY created_at DESC LIMIT 1
"""

_LATEST_INBOUND_SQL = """
SELECT id, lead_id, tenant_id, direction, message, created_at FROM public.sms_log
WHERE tenant_id = $1 AND thread_key = ANY($2::text[]) AND direction = 'inbound'
ORDER BY created_at DESC, id DESC LIMIT 1
"""

_RECENT_SMS_SQL = """
SELECT id, created_at, direction, message, sent_by, cadence_id FROM public.sms_log
WHERE tenant_id = $1 AND thread_key = ANY($2::text[])
ORDER BY created_at DESC, id DESC LIMIT $3
"""


@dataclass
class ToolContext:
    """Everything a tool may need about this request. Database lookups run on first use and are cached, so a
    request whose model calls no tool costs no query."""

    settings: ToolsConfig
    tenant_id: Optional[UUID]
    sms_thread_key: Optional[str]
    email_thread_key: Optional[str]
    turns: list[dict[str, Any]]  # the request's non-system messages
    pool: Optional[asyncpg.Pool] = None
    http: Optional[httpx.AsyncClient] = None
    retriever: Optional[Retriever] = None
    # Outputs of the workflow's upstream nodes the caller supplied (request.node_data). Each lookup below
    # returns the supplied node first, the way n8n's Tool Context node reads it, and falls back to the database.
    node_data: dict[str, Any] = field(default_factory=dict)
    now_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Appointment times Validate_Appointment confirmed during this completion (confirmed_datetime -> result).
    validated_appointments: dict[str, dict[str, Any]] = field(default_factory=dict)
    _cache: dict[str, Any] = field(default_factory=dict)

    # ---- what the request itself says -------------------------------------------------------------------

    @property
    def live(self) -> bool:
        return self.settings.write_mode == "live"

    @property
    def inbound_text(self) -> str:
        """The customer's latest message (n8n "Normalize Phone".text)."""
        supplied = nd.value(self.node_data, "Normalize Phone", "text")
        if nd.present(supplied):
            return str(supplied)
        return next((str(t["content"]) for t in reversed(self.turns) if t["role"] == "user"), "")

    @property
    def combined_message(self) -> str:
        """The customer's messages since our last reply (n8n "Combine messages")."""
        supplied = nd.value(self.node_data, "Combine messages", "combinedMessage")
        if nd.present(supplied):
            return str(supplied)
        texts: list[str] = []
        for t in reversed(self.turns):
            if t["role"] != "user":
                break
            texts.append(str(t["content"]))
        return "\n".join(reversed(texts))

    @property
    def last_outbound(self) -> str:
        """Our last message before the customer's latest one."""
        seen_user = False
        for t in reversed(self.turns):
            if t["role"] == "user":
                seen_user = True
            elif seen_user and t["role"] == "assistant":
                return str(t["content"])
        return ""

    @property
    def source(self) -> str:
        return "sms" if self.sms_thread_key or not self.email_thread_key else "email"

    def phones(self) -> list[str]:
        return phone_variants(self.sms_thread_key) if self.sms_thread_key else []

    def emails(self) -> list[str]:
        return email_variants(self.email_thread_key) if self.email_thread_key else []

    # ---- lookups (cached per request) -------------------------------------------------------------------

    def need_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise ToolFailure("the dealership database is not reachable")
        return self.pool

    async def _cached(self, key: str, load: Callable[[], Awaitable[Any]]) -> Any:
        if key not in self._cache:
            self._cache[key] = await load()
        return self._cache[key]

    def supplied(self, node: str) -> dict[str, Any]:
        """A supplied node's output object, {} when it was not supplied."""
        value = nd.node(self.node_data, node)
        return value if isinstance(value, dict) else {}

    @property
    def has_supplied_lead(self) -> bool:
        return self.tenant_id is not None and nd.present(self.supplied("Lookup Lead").get("id"))

    def classifier(self) -> Optional[dict[str, Any]]:
        """The supplied "Intent Classifier" output; None when not supplied (it has no equivalent here)."""
        value = nd.node(self.node_data, "Intent Classifier")
        return value if isinstance(value, dict) else None

    async def lead(self) -> dict[str, Any]:
        """The customer's lead (n8n "Lookup Lead"). A supplied lead with an id is used as is; supplied fields
        without an id are laid over the looked-up one. Raises ToolFailure when there is none."""
        supplied = self.supplied("Lookup Lead")

        async def load() -> Optional[dict[str, Any]]:
            if self.has_supplied_lead:  # tenant_id is always the request's: the request model rejects any other
                return {**supplied, "id": str(supplied["id"]), "tenant_id": self.tenant_id}
            row = None
            if self.tenant_id is not None and (self.phones() or self.emails()):
                row = await self.need_pool().fetchrow(_LEAD_SQL, self.tenant_id, self.phones(), self.emails())
            if not row:
                return None
            return {**dict(row), **{k: v for k, v in supplied.items() if k not in ("id", "tenant_id")}}

        lead = await self._cached("lead", load)
        if lead is None:
            raise ToolFailure("no lead on file for this customer at this dealership")
        return lead

    async def tenant(self) -> dict[str, Any]:
        """The dealership row, {} when unknown. Never raises: callers fall back to defaults."""
        async def load() -> dict[str, Any]:
            if self.tenant_id is None or self.pool is None:
                return {}
            try:
                row = await self.pool.fetchrow(_TENANT_SQL, self.tenant_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("tool context: tenant lookup failed: {}", f"{type(exc).__name__}: {exc}"[:200])
                return {}
            if not row:
                return {}
            tenant = dict(row)
            tenant["hours_of_operation"] = _json(tenant.get("hours_of_operation")) or {}
            return tenant

        async def merged() -> dict[str, Any]:
            # n8n "Get tenant" and "Get tenant emails & phone": supplied fields win over the dealership row.
            tenant = {**(await load()), **self.supplied("Get tenant"), **self.supplied("Get tenant emails & phone")}
            tenant["hours_of_operation"] = _json(tenant.get("hours_of_operation")) or {}
            return tenant

        return await self._cached("tenant", merged)

    async def timezone_name(self) -> str:
        """The dealership's timezone, in the workflow's order: hours_of_operation.timezone, timezone, time_zone."""
        tenant = await self.tenant()
        hours = tenant.get("hours_of_operation") or {}
        candidates = [hours.get("timezone") if isinstance(hours, dict) else None, tenant.get("timezone"), tenant.get("time_zone")]
        for name in candidates:
            if nd.present(name):
                try:
                    ZoneInfo(str(name))
                    return str(name)
                except (ZoneInfoNotFoundError, ValueError):
                    break
        return DEFAULT_TIMEZONE

    async def vehicle(self) -> Optional[dict[str, Any]]:
        """The vehicle this conversation is about (n8n "Lookup Lead Vehicle"), None when the lead has none. A supplied
        "Lookup Lead Vehicle" is used as is."""
        async def load() -> Optional[dict[str, Any]]:
            if self.supplied("Lookup Lead Vehicle"):
                return self.supplied("Lookup Lead Vehicle")
            lead = await self.lead()
            pool = self.need_pool()
            try:
                value = await pool.fetchval(_RESOLVE_VEHICLE_SQL, lead["id"], lead["tenant_id"], self.inbound_text)
                if value is not None:
                    return _json(value)
            except Exception as exc:  # noqa: BLE001 - older databases lack the resolver
                logger.info("tool context: resolve_thread_vehicle unavailable ({}), using the primary vehicle", type(exc).__name__)
            row = await pool.fetchrow(_PRIMARY_VEHICLE_SQL, lead["id"])
            return dict(row) if row else None

        return await self._cached("vehicle", load)

    async def active_cadence(self) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            lead = await self.lead()
            row = await self.need_pool().fetchrow(_ACTIVE_CADENCE_SQL, lead["id"], lead["tenant_id"])
            return dict(row) if row else {}

        return await self._cached("cadence", load)

    async def saved_inbound(self) -> Optional[dict[str, Any]]:
        """The sms_log row of the customer's latest message, when the caller already stored it (n8n "Insert Log").
        Only a row whose text is this turn's message counts: it is the receipt that this turn really happened.
        A supplied "Insert Log" row is taken as that receipt."""
        async def load() -> Optional[dict[str, Any]]:
            if nd.present(self.supplied("Insert Log").get("id")):
                return self.supplied("Insert Log")
            if self.tenant_id is None or not self.phones() or self.pool is None:
                return None
            row = await self.pool.fetchrow(_LATEST_INBOUND_SQL, self.tenant_id, self.phones())
            if not row or _norm(row["message"]) != _norm(self.inbound_text):
                return None
            return dict(row)

        return await self._cached("saved_inbound", load)

    async def recent_sms(self, limit: int) -> list[dict[str, Any]]:
        """The thread's newest `limit` SMS messages, oldest first. A supplied "Get Recent SMS History" is used the way
        n8n's Tool Context node uses it: rows with a message and a direction, ordered by created_at."""
        supplied = nd.node(self.node_data, "Get Recent SMS History")
        if isinstance(supplied, list):
            rows = [r for r in supplied if isinstance(r, dict) and isinstance(r.get("message"), str) and r.get("direction")]
            rows.sort(key=lambda r: str(r.get("created_at") or ""))
            return rows[-limit:] if limit > 0 else []
        if self.tenant_id is None or not self.phones():
            return []
        rows = await self.need_pool().fetch(_RECENT_SMS_SQL, self.tenant_id, self.phones(), limit)
        return [dict(r) for r in reversed(rows)]


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()

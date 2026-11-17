"""
Read-only access to the customer's own records in the ai-agent-management
Postgres: the SMS and email history (sms_log, email_log), the customer's leads
and their vehicles (leads, lead_vehicles), and the dealership (tenants).

A customer is the tenant plus their SMS number and/or email address. One
number or address can belong to several leads, and each lead to several
vehicles, so everything is gathered across all of them.

A thread can hold ~10K messages, so it is never read whole. Per channel:
- counts, first/last contact, opt-out and per-campaign totals, aggregated in SQL;
- the newest `recent_messages`, verbatim;
- older messages that match the customer's latest message (pg_trgm), so an
  old answer (a quoted value, an appointment) still reaches the model;
- older non-campaign messages (customer replies, staff/AI answers), newest
  first. Campaign blasts are templates; they are summarised by the counts.
All of it comes from the (tenant_id, thread_key, created_at) indexes, in one
parallel round trip.
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

import asyncpg
from loguru import logger

from app.llm.settings import CustomerConfig

_RECONNECT_EVERY = 30.0

_STOP_RE = r"^\s*(stop|stopall|stop all|unsubscribe|cancel|end|quit|opt[ -]?out)\W*$"
_START_RE = r"^\s*(start|unstop|resume)\W*$"


@dataclass(frozen=True)
class _Channel:
    name: str
    table: str
    match: str  # binds $1 tenant_id, $2 text[] of the customer's keys
    body: str
    subject: str


_SMS = _Channel(
    "sms",
    "public.sms_log",
    "m.tenant_id = $1 AND m.thread_key = ANY($2::text[])",
    "m.message",
    "NULL::text",
)

# Outbound mail has the customer in email_to, inbound in email_from, either as `addr` or `Name <addr>`.
_EMAIL = _Channel(
    "email",
    "public.email_log",
    "m.tenant_id = $1 AND ("
    "lower(m.email_to) = ANY($2::text[]) OR lower(m.email_from) = ANY($2::text[]) "
    "OR lower(substring(m.email_to from '<([^>]+)>')) = ANY($2::text[]) "
    "OR lower(substring(m.email_from from '<([^>]+)>')) = ANY($2::text[]))",
    "coalesce(nullif(m.body_text, ''), m.body_html)",
    "m.subject",
)

_COLUMNS = """m.id::text AS id, m.created_at, m.direction, {body} AS body, {subject} AS subject,
              m.cadence_id, m.sent_by, c.name AS campaign"""

_FROM = "FROM {table} m LEFT JOIN public.cadences c ON c.id = m.cadence_id WHERE {match}"

_STATS_SQL = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE m.direction = 'inbound') AS inbound,
       min(m.created_at) AS first_at,
       max(m.created_at) AS last_at,
       max(m.created_at) FILTER (WHERE m.direction = 'inbound' AND {body} ~* '%s') AS stopped_at,
       max(m.created_at) FILTER (WHERE m.direction = 'inbound' AND {body} ~* '%s') AS restarted_at
FROM {table} m WHERE {match}
""" % (_STOP_RE, _START_RE)

_CAMPAIGNS_SQL = """
SELECT coalesce(c.name, 'campaign') AS campaign, count(*) AS sent, min(m.created_at) AS first_at, max(m.created_at) AS last_at
""" + _FROM + """
  AND m.direction <> 'inbound' AND m.cadence_id IS NOT NULL
GROUP BY 1 ORDER BY max(m.created_at) DESC
LIMIT 20
"""

_RECENT_SQL = "SELECT " + _COLUMNS + " " + _FROM + " ORDER BY m.created_at DESC, m.id DESC LIMIT $3"

# Messages older than the recent window ($3 = its size), numbered newest first, so every query of a
# thread runs in the same round trip instead of waiting for the recent window.
_NUMBERED = "WITH t AS (SELECT " + _COLUMNS + ", row_number() OVER (ORDER BY m.created_at DESC, m.id DESC) AS rn " + _FROM + ")"

_RELEVANT_SQL = (
    _NUMBERED + " SELECT *, word_similarity($4, body) AS score FROM t WHERE rn > $3 AND body IS NOT NULL"
    " ORDER BY score DESC, rn LIMIT $5"
)

_OLDER_SQL = (
    _NUMBERED + " SELECT * FROM t WHERE rn > $3 AND (direction = 'inbound' OR cadence_id IS NULL) ORDER BY rn LIMIT $4"
)

_LEADS_SQL = """
SELECT id, first_name, last_name, email, phone, status, dnc, dnc_reason, blocked, rep_name, timezone, created_at
FROM public.leads
WHERE tenant_id = $1
  AND (phone = ANY($2::text[]) OR btrim(thread_id) = ANY($2::text[]) OR lower(email) = ANY($3::text[]))
ORDER BY created_at DESC
LIMIT 50
"""

_VEHICLES_SQL = """
SELECT lead_id, year, make, model, trim, mileage, mileage_update_date, vin, market_value, market_value_date,
       is_primary, ownership_status, lease_loan_cash, purchase_date, "isSold" AS is_sold, date_sold, created_at
FROM public.lead_vehicles
WHERE lead_id IN (
    SELECT id FROM public.leads
    WHERE tenant_id = $1
      AND (phone = ANY($2::text[]) OR btrim(thread_id) = ANY($2::text[]) OR lower(email) = ANY($3::text[]))
)
ORDER BY is_primary DESC NULLS LAST, created_at DESC
LIMIT $4
"""

_TENANT_SQL = """
SELECT coalesce(nullif(company_legal_name, ''), nullif(email_from_name, ''), slug) AS name,
       address, city, state, zipcode, main_phone, hours_of_operation
FROM public.tenants WHERE id = $1
"""


@dataclass
class Message:
    id: str
    created_at: datetime
    direction: str
    body: str
    subject: Optional[str]
    cadence_id: Optional[UUID]
    sent_by: Optional[str]
    campaign: Optional[str]


@dataclass
class Campaign:
    name: str
    sent: int
    first_at: datetime
    last_at: datetime


@dataclass
class Thread:
    channel: str
    total: int = 0
    inbound: int = 0
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    restarted_at: Optional[datetime] = None
    campaigns: list[Campaign] = field(default_factory=list)
    recent: list[Message] = field(default_factory=list)  # oldest first
    relevant: list[Message] = field(default_factory=list)  # older than `recent`, best match first
    older: list[Message] = field(default_factory=list)  # older than `recent`, newest first

    @property
    def opted_out(self) -> bool:
        return self.stopped_at is not None and (self.restarted_at is None or self.restarted_at < self.stopped_at)


@dataclass
class Lead:
    id: UUID
    first_name: Optional[str]
    last_name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    status: Optional[str]
    dnc: bool
    dnc_reason: Optional[str]
    blocked: bool
    rep_name: Optional[str]
    timezone: Optional[str]


@dataclass
class Vehicle:
    lead_id: UUID
    year: Optional[int]
    make: Optional[str]
    model: Optional[str]
    trim: Optional[str]
    mileage: Optional[int]
    mileage_date: Optional[date]
    vin: Optional[str]
    market_value: Optional[Decimal]
    market_value_date: Optional[date]
    is_primary: bool
    ownership_status: Optional[str]
    lease_loan_cash: Optional[str]
    purchase_date: Optional[date]
    is_sold: bool
    date_sold: Optional[date]


@dataclass
class Dealership:
    name: Optional[str]
    address: Optional[str]
    main_phone: Optional[str]
    hours: Optional[dict]


@dataclass
class CustomerRecords:
    status: str  # ok | skipped | error
    reason: str = ""
    dealership: Optional[Dealership] = None
    leads: list[Lead] = field(default_factory=list)
    vehicles: list[Vehicle] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)


def phone_variants(raw: str) -> list[str]:
    """sms_log keys are E.164 (+15551234567) but leads.phone is stored several ways; match them all."""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    variants = {raw}
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        variants |= {digits, "1" + digits, "+1" + digits}
    elif digits:
        variants |= {digits, "+" + digits}
    return sorted(variants)


def email_variants(raw: str) -> list[str]:
    match = re.search(r"<([^>]+)>", raw)
    return [(match.group(1) if match else raw).strip().lower()]


class CustomerStore:
    def __init__(self, settings: CustomerConfig, dsn: str) -> None:
        self.settings = settings
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def start(self) -> None:
        """Never raises: chat works without the customer's records."""
        self._last_attempt = time.monotonic()
        try:
            # statement_cache_size=0: PG_DB_URL is Supavisor's transaction pooler, which can't keep prepared statements.
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=2, max_size=12, statement_cache_size=0, command_timeout=self.settings.timeout
            )
            logger.info("Customer records ready (history budget {}tok)", self.settings.history_tokens)
        except Exception as exc:  # noqa: BLE001
            logger.error("Customer records unavailable: {}", f"{type(exc).__name__}: {exc}"[:300])

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def load(
        self, tenant_id: UUID, sms_key: Optional[str], email_key: Optional[str], query: str
    ) -> CustomerRecords:
        if not sms_key and not email_key:
            return CustomerRecords("skipped", "no sms_thread_key or email_thread_key")
        if not self.ready and not await self._reconnect():
            return CustomerRecords("error", "customer records unavailable")
        started = time.perf_counter()
        try:
            records = await asyncio.wait_for(self._load(tenant_id, sms_key, email_key, query), self.settings.timeout)
        except asyncio.TimeoutError:
            logger.warning("Customer records timeout after {}s tenant={}", self.settings.timeout, tenant_id)
            return CustomerRecords("error", "timeout")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Customer records failed tenant={}: {}", tenant_id, f"{type(exc).__name__}: {exc}"[:300])
            return CustomerRecords("error", type(exc).__name__)
        logger.info(
            "Customer records tenant={} leads={} vehicles={} {} ms={}",
            tenant_id, len(records.leads), len(records.vehicles),
            " ".join(f"{t.channel}={t.total}" for t in records.threads), int((time.perf_counter() - started) * 1000),
        )
        return records

    async def _load(self, tenant_id: UUID, sms_key: Optional[str], email_key: Optional[str], query: str) -> CustomerRecords:
        phones = phone_variants(sms_key) if sms_key else []
        emails = email_variants(email_key) if email_key else []
        jobs = [self._dealership(tenant_id), self._leads_and_vehicles(tenant_id, phones, emails)]
        if phones:
            jobs.append(self._thread(_SMS, tenant_id, phones, query))
        if emails:
            jobs.append(self._thread(_EMAIL, tenant_id, emails, query))
        dealership, (leads, vehicles), *threads = await asyncio.gather(*jobs)
        return CustomerRecords("ok", dealership=dealership, leads=leads, vehicles=vehicles, threads=threads)

    async def _dealership(self, tenant_id: UUID) -> Optional[Dealership]:
        row = await self._pool.fetchrow(_TENANT_SQL, tenant_id)
        if row is None:
            return None
        address = ", ".join(p for p in (row["address"], row["city"], row["state"], row["zipcode"]) if p)
        hours = row["hours_of_operation"]
        if isinstance(hours, str):
            try:
                hours = json.loads(hours)
            except ValueError:
                hours = None
        return Dealership(row["name"], address or None, row["main_phone"], hours if isinstance(hours, dict) else None)

    async def _leads_and_vehicles(self, tenant_id: UUID, phones: list[str], emails: list[str]) -> tuple[list[Lead], list[Vehicle]]:
        lead_rows, rows = await asyncio.gather(
            self._pool.fetch(_LEADS_SQL, tenant_id, phones, emails),
            self._pool.fetch(_VEHICLES_SQL, tenant_id, phones, emails, self.settings.max_vehicles),
        )
        leads = [
            Lead(r["id"], r["first_name"], r["last_name"], r["email"], r["phone"], r["status"], bool(r["dnc"]),
                 r["dnc_reason"], bool(r["blocked"]), r["rep_name"], r["timezone"])
            for r in lead_rows
        ]
        vehicles, seen = [], set()
        for r in rows:
            # The same car is often on file under each of the customer's leads.
            key = (r["vin"] or "").strip().upper() or (r["year"], (r["make"] or "").lower(), (r["model"] or "").lower())
            if key in seen:
                continue
            seen.add(key)
            value = r["market_value"]
            vehicles.append(Vehicle(
                r["lead_id"], r["year"], r["make"], r["model"], r["trim"], r["mileage"], r["mileage_update_date"],
                r["vin"], value if value else None, r["market_value_date"] if value else None, bool(r["is_primary"]),
                r["ownership_status"], r["lease_loan_cash"], r["purchase_date"], bool(r["is_sold"]), r["date_sold"],
            ))
        return leads, vehicles

    async def _thread(self, ch: _Channel, tenant_id: UUID, keys: list[str], query: str) -> Thread:
        s = self.settings
        fmt = {"table": ch.table, "match": ch.match, "body": ch.body, "subject": ch.subject}
        jobs = [
            self._pool.fetchrow(_STATS_SQL.format(**fmt), tenant_id, keys),
            self._pool.fetch(_CAMPAIGNS_SQL.format(**fmt), tenant_id, keys),
            self._pool.fetch(_RECENT_SQL.format(**fmt), tenant_id, keys, s.recent_messages),
            self._pool.fetch(_OLDER_SQL.format(**fmt), tenant_id, keys, s.recent_messages, s.older_messages),
        ]
        if query and s.relevant_messages:
            jobs.append(self._relevant(fmt, tenant_id, keys, query))
        stats, campaigns, recent, older, *relevant = await asyncio.gather(*jobs)
        return Thread(
            ch.name, stats["total"], stats["inbound"], stats["first_at"], stats["last_at"], stats["stopped_at"],
            stats["restarted_at"],
            campaigns=[Campaign(r["campaign"], r["sent"], r["first_at"], r["last_at"]) for r in campaigns],
            recent=[_message(r) for r in reversed(recent)],
            relevant=relevant[0] if relevant else [],
            older=[_message(r) for r in older],
        )

    async def _relevant(self, fmt: dict, tenant_id: UUID, keys: list[str], query: str) -> list[Message]:
        s = self.settings
        try:
            rows = await self._pool.fetch(
                _RELEVANT_SQL.format(**fmt), tenant_id, keys, s.recent_messages, query[:500], s.relevant_messages
            )
        except asyncpg.UndefinedFunctionError:  # pg_trgm missing: older context still comes from _OLDER_SQL
            return []
        return [_message(r) for r in rows if r["score"] >= 0.3]

    async def store_exchange(self, tenant_id: UUID, sms_key: Optional[str], email_key: Optional[str],
                             customer_text: str, reply_text: str, sent_by: Optional[str], cnp_provider: Optional[str],
                             step_num: Optional[int] = None):
        """Write the customer's message and the reply to sms_log / email_log (see app/customer/writer.py)."""
        from app.customer.writer import StoreError, store_exchange

        if not self.ready and not await self._reconnect():
            raise StoreError("customer records database unavailable")
        return await asyncio.wait_for(
            store_exchange(self._pool, tenant_id, sms_key, email_key, customer_text, reply_text, sent_by, cnp_provider, step_num),
            self.settings.timeout,
        )

    async def _reconnect(self) -> bool:
        async with self._lock:
            if not self.ready and time.monotonic() - self._last_attempt >= _RECONNECT_EVERY:
                await self.start()
        return self.ready


def _message(r) -> Message:
    return Message(r["id"], r["created_at"], r["direction"], r["body"] or "", r["subject"], r["cadence_id"], r["sent_by"], r["campaign"])

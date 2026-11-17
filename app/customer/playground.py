"""
Read-only lookups for the web UI's chat tester: dealerships, the customers who
have an SMS or email thread with one, and a thread's messages a page at a time.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from app.customer.context import _clean
from app.customer.store import _COLUMNS, _EMAIL, _FROM, _SMS, email_variants, phone_variants

_TENANTS_SQL = """
SELECT id, coalesce(nullif(company_legal_name, ''), nullif(email_from_name, ''), slug) AS name
FROM public.tenants ORDER BY 2
"""

# The customer's side of each message: thread_key for SMS; for email, the other party's bare address.
_EMAIL_CUSTOMER = (
    "lower(btrim(coalesce(substring({col} from '<([^>]+)>'), {col})))".format(
        col="CASE WHEN m.direction = 'inbound' THEN m.email_from ELSE m.email_to END"
    )
)

# Newest message per customer first, across every dealership unless $1 names one. $3 is a search text
# matched against the key, the lead's name and the dealership's name.
_SMS_THREADS_SQL = """
WITH last AS (
    SELECT DISTINCT ON (m.tenant_id, m.thread_key) m.tenant_id, m.thread_key AS key, m.created_at AS last_at,
           m.direction, m.message AS body
    FROM public.sms_log m
    WHERE ($1::uuid IS NULL OR m.tenant_id = $1) AND m.thread_key IS NOT NULL AND m.thread_key <> ''
    ORDER BY m.tenant_id, m.thread_key, m.created_at DESC
), names AS (
    -- One pass over the leads (they have no tenant/phone index), joined by hash rather than per thread.
    SELECT DISTINCT ON (l.tenant_id, v.key) l.tenant_id, v.key, btrim(concat_ws(' ', l.first_name, l.last_name)) AS name
    FROM public.leads l, LATERAL (VALUES (btrim(l.thread_id)), (l.phone)) v(key)
    WHERE ($1::uuid IS NULL OR l.tenant_id = $1) AND v.key IS NOT NULL
    ORDER BY l.tenant_id, v.key, l.created_at DESC
)
SELECT last.*, names.name, {tenant_name} AS tenant_name
FROM last
LEFT JOIN names ON names.tenant_id = last.tenant_id AND names.key = last.key
LEFT JOIN public.tenants t ON t.id = last.tenant_id
WHERE $3::text IS NULL OR last.key ILIKE '%' || $3 || '%' OR names.name ILIKE '%' || $3 || '%'
   OR {tenant_name} ILIKE '%' || $3 || '%'
ORDER BY last.last_at DESC
LIMIT $2
"""

_EMAIL_THREADS_SQL = """
WITH last AS (
    SELECT DISTINCT ON (m.tenant_id, 2) m.tenant_id, {customer} AS key, m.created_at AS last_at, m.direction,
           coalesce(nullif(m.subject, ''), nullif(m.body_text, ''), m.body_html) AS body
    FROM public.email_log m
    WHERE ($1::uuid IS NULL OR m.tenant_id = $1)
    ORDER BY m.tenant_id, 2, m.created_at DESC
), names AS (
    SELECT DISTINCT ON (l.tenant_id, lower(l.email)) l.tenant_id, lower(l.email) AS key,
           btrim(concat_ws(' ', l.first_name, l.last_name)) AS name
    FROM public.leads l
    WHERE ($1::uuid IS NULL OR l.tenant_id = $1) AND l.email IS NOT NULL
    ORDER BY l.tenant_id, lower(l.email), l.created_at DESC
)
SELECT last.*, names.name, {tenant_name} AS tenant_name
FROM last
LEFT JOIN names ON names.tenant_id = last.tenant_id AND names.key = last.key
LEFT JOIN public.tenants t ON t.id = last.tenant_id
WHERE last.key <> '' AND ($3::text IS NULL OR last.key ILIKE '%' || $3 || '%' OR names.name ILIKE '%' || $3 || '%'
   OR {tenant_name} ILIKE '%' || $3 || '%')
ORDER BY last.last_at DESC
LIMIT $2
"""

_TENANT_NAME = "coalesce(nullif(t.company_legal_name, ''), nullif(t.email_from_name, ''), t.slug)"
_SMS_THREADS_SQL = _SMS_THREADS_SQL.format(tenant_name=_TENANT_NAME)
_EMAIL_THREADS_SQL = _EMAIL_THREADS_SQL.format(customer=_EMAIL_CUSTOMER, tenant_name=_TENANT_NAME)

# One page of a thread, newest first; $3 is the page size, $4 an optional "older than" cursor.
_HISTORY_SQL = (
    "SELECT " + _COLUMNS + " " + _FROM
    + " AND ($4::timestamptz IS NULL OR m.created_at < $4) ORDER BY m.created_at DESC, m.id DESC LIMIT $3"
)


async def tenants(pool: asyncpg.Pool) -> list[dict]:
    return [{"id": str(r["id"]), "name": r["name"] or str(r["id"])} for r in await pool.fetch(_TENANTS_SQL)]


async def threads(pool: asyncpg.Pool, tenant_id: Optional[UUID], channel: str, search: Optional[str], limit: int) -> list[dict]:
    sql = _SMS_THREADS_SQL if channel == "sms" else _EMAIL_THREADS_SQL
    rows = await pool.fetch(sql, tenant_id, limit, (search or "").strip() or None)
    return [
        {
            "tenant_id": str(r["tenant_id"]),
            "tenant_name": r["tenant_name"] or None,
            "key": r["key"],
            "name": r["name"] or None,
            "last_at": r["last_at"],
            "last_direction": r["direction"],
            "last_message": _clean(r["body"] or "", email=channel == "email")[:160],
        }
        for r in rows
    ]


async def history(
    pool: asyncpg.Pool, tenant_id: UUID, channel: str, key: str, limit: int, before: Optional[datetime]
) -> dict:
    """Oldest first, plus whether there is an older page (fetch it with before = the first message's created_at)."""
    ch = _SMS if channel == "sms" else _EMAIL
    keys = phone_variants(key) if channel == "sms" else email_variants(key)
    sql = _HISTORY_SQL.format(table=ch.table, match=ch.match, body=ch.body, subject=ch.subject)
    rows = await pool.fetch(sql, tenant_id, keys, limit + 1, before)
    page = rows[:limit]
    return {
        "has_more": len(rows) > limit,
        "messages": [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "direction": r["direction"],
                "subject": r["subject"],
                "body": _clean(r["body"] or "", email=True) if channel == "email" else (r["body"] or ""),
                "sent_by": r["sent_by"],
                "campaign": r["campaign"] if r["cadence_id"] else None,
            }
            for r in reversed(page)
        ],
    }

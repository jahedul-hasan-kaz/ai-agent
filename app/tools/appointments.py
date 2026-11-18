"""
Appointment tools of the scheduling (and value) agents.

* Validate_Appointment (n8n Code tool): is a proposed day+time in the future, at least an hour out and inside the
  dealership's hours? Or, for "what times do you have Monday?", the next open slot. A port of the n8n code with
  the same parsing rules, guards and reply shapes; the clock is the dealership's own time zone.
* Save_Appointment_Notes (n8n HTTP tool, a PostgREST upsert): upserts appointment_notes on (lead_id, cadence_id).
  A time is only booked when Validate_Appointment confirmed exactly that time in this same completion.
* Cancel_Appointment (n8n Code tool): public.cancel_appointment, the only writer of status='cancelled'.
"""
import calendar
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.tools.base import NEEDS_CUSTOMER, NEEDS_DB, Tool, ToolContext, ToolFailure, schema

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]  # Python weekday() order
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTHS["sept"] = 9
_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# The tenants.hours_of_operation column default, used when the dealership's own hours are unknown.
DEFAULT_HOURS = {
    "monday": {"open": "09:00", "close": "20:00"}, "tuesday": {"open": "09:00", "close": "20:00"},
    "wednesday": {"open": "09:00", "close": "20:00"}, "thursday": {"open": "09:00", "close": "20:00"},
    "friday": {"open": "09:00", "close": "20:00"}, "saturday": {"open": "10:00", "close": "17:00"},
    "sunday": {"open": "12:00", "close": "17:00"}, "timezone": "America/New_York",
}
MIN_LEAD_TIME = timedelta(minutes=60)
_WEEKDAY = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"


# ---------------------------------------------------------------------------------------------------------------
# Validate_Appointment
# ---------------------------------------------------------------------------------------------------------------

async def validate_appointment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    tenant = await ctx.tenant()
    hours = tenant.get("hours_of_operation") or DEFAULT_HOURS
    tz_name = await ctx.timezone_name()
    now = ctx.now_utc.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None, second=0, microsecond=0)
    result = check_appointment(args, hours, now, tz_name, ctx.inbound_text, ctx.combined_message)
    if result.get("is_valid") and result.get("confirmed_datetime"):
        ctx.validated_appointments[result["confirmed_datetime"]] = result
    return result


def check_appointment(
    args: dict[str, Any], hours: dict[str, Any], now: datetime, tz_name: str, inbound_text: str = "", combined: str = ""
) -> dict[str, Any]:
    """The n8n validator. `now` is the dealership's wall clock (naive)."""
    proposed_text = str(
        _first(args, "proposed_time", "proposedTime", "query", "input", "text") or inbound_text or combined or ""
    ).strip()
    availability_date = str(_first(args, "availability_date", "availabilityDate", "date_reference", "dateReference") or "").strip()
    availability_text = str(_first(args, "availability_request_text", "availabilityRequestText") or proposed_text).strip()
    looks_like_availability = bool(args.get("availability_request") or args.get("availabilityRequest")) or (
        re.search(r"\b(available|availability|openings?|slots?|times?)\b", availability_text, re.I) is not None
        and re.search(
            r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2})\b",
            availability_text, re.I,
        ) is not None
    )
    reference = availability_date or (availability_text if looks_like_availability else "")
    if availability_date or looks_like_availability:
        return _availability(reference, now, hours)

    if not _has_time_anchor(proposed_text):
        return {
            "is_valid": False, "reason": "no_time_anchor",
            "message": "The customer's message contains no clock time, day word, weekday, or near-term anchor. Do NOT "
            "propose any clock time. Either acknowledge their statement and close warmly, ask what day and time works "
            "best for them (open question, no specific slot), or escalate per the prompt rules. Do NOT call this tool "
            "again for this turn.",
            "requested_text": proposed_text, "next_available": None,
        }

    if _is_far_future(proposed_text, now):
        return {
            "is_valid": False, "reason": "far_future_request",
            "message": f'The customer is asking to be contacted in a far-future window ("{proposed_text}"), not now. '
            "Acknowledge that timeframe verbatim and END the conversation. Do NOT propose any near-term slot. Do NOT "
            "call this tool again for this turn.",
            "requested_timeframe": proposed_text, "next_available": None,
        }

    proposed = parse_proposed_time(proposed_text, now)
    if proposed is None:
        date_only = parse_date_reference(proposed_text, now)
        if date_only is not None:
            label, iso = _day_label(date_only), date_only.strftime("%Y-%m-%d")
            return {
                "is_valid": False, "reason": "date_only_no_time",
                "message": f"{iso} falls on {label}. The customer mentioned a date but no clock time. Use this weekday "
                "verbatim if you need to name a day. If you need to schedule, ask the customer for a specific time "
                "within business hours — do NOT guess one.",
                "day_of_week": label, "proposed_date": iso, "next_available": None,
            }
        return {
            "is_valid": False, "reason": "could_not_parse",
            "message": f'Could not understand the proposed time: "{proposed_text}". Ask the customer to clarify with a '
            "specific day and time. Do NOT guess. Do NOT propose any time.",
            "next_available": None,
        }

    label, iso = _day_label(proposed), proposed.strftime("%Y-%m-%d")
    earliest = now + MIN_LEAD_TIME
    if proposed <= now:
        return {
            "is_valid": False, "reason": "past",
            "message": f"The proposed time ({format_for_display(proposed)}) has already passed.",
            "day_of_week": label, "proposed_date": iso, "next_available": next_available(earliest, hours, now),
        }
    if proposed < earliest:
        return {
            "is_valid": False, "reason": "too_soon", "requires_escalation": True,
            "message": f"The proposed time ({format_for_display(proposed)}) is less than an hour from now. Escalate "
            "instead of offering a later slot.",
            "day_of_week": label, "proposed_date": iso, "next_available": None,
        }

    day = _day_hours(hours, proposed)
    if day is None:
        return {
            "is_valid": False, "reason": "outside_hours",
            "message": f"The dealership is closed on {label}.",
            "day_of_week": label, "proposed_date": iso, "next_available": next_available(proposed, hours, now),
        }
    open_m, close_m = day
    minutes = proposed.hour * 60 + proposed.minute
    if minutes < open_m or minutes >= close_m:
        return {
            "is_valid": False, "reason": "outside_hours",
            "message": f"The proposed time is outside business hours. On {label}, hours are "
            f"{_format_12h(open_m)} - {_format_12h(close_m)}.",
            "day_of_week": label, "proposed_date": iso, "next_available": next_available(proposed, hours, now),
        }

    return {
        "is_valid": True, "reason": None,
        "message": f"Appointment is valid: {format_for_display(proposed)} ({tz_name}). The proposed time falls on "
        f"{label} ({iso}).",
        "confirmed_datetime": to_utc_iso(proposed, tz_name),
        "confirmed_display": format_for_display(proposed),
        "day_of_week": label, "proposed_date": iso, "timezone": tz_name, "next_available": None,
    }


def _first(args: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if args.get(key) not in (None, ""):
            return args[key]
    return None


def _has_time_anchor(text: str) -> bool:
    t = text.strip()
    return any(re.search(p, t, re.I) for p in (
        r"\b\d{1,2}(?::\d{2})?\s*(am|pm|a\.m|p\.m)\b", r"\b\d{1,2}:\d{2}\b",
        r"\b(today|tonight|tomorrow|noon|midnight|this\s+(morning|afternoon|evening|week))\b",
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\b\d{4}-\d{2}-\d{2}\b", r"\b\d{1,2}/\d{1,2}\b", r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\s+\d{1,2}\b",
        r"\bin\s+\d{1,2}\s*(min|mins|minute|minutes|hour|hours|hr|hrs)\b",
        r"\b(later\s+(today|tonight)|right\s+now|asap|soon\s+as\s+possible)\b",
    ))


def _is_far_future(text: str, now: datetime) -> bool:
    if any(int(y) > now.year for y in re.findall(r"\b(20\d{2})\b", text)):
        return True
    return any(re.search(p, text, re.I) for p in (
        r"\b(beginning|start|end|middle)\s+of\s+20\d{2}\b", r"\b(early|late|mid)\s+20\d{2}\b", r"\bnext\s+year\b",
        r"\bin\s+([3-9]|1[0-2])\s+months?\b", r"\bin\s+\d+\s+years?\b", r"\bafter\s+(?:the\s+)?(?:new\s+year|holidays)\b",
    ))


def apply_am_pm(hour: int, period: Optional[str]) -> int:
    """No am/pm: 9-11 is morning, 12 is noon and 1-8 is afternoon (nobody books a 3am appraisal)."""
    if period == "pm" and hour < 12:
        return hour + 12
    if period == "am" and hour == 12:
        return 0
    if not period and 1 <= hour <= 8:
        return hour + 12
    return hour


def _at(day: datetime, hour: int, minute: int) -> Optional[datetime]:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _date(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> Optional[datetime]:
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def next_weekday(start: datetime, weekday: int) -> datetime:
    """The next `weekday` strictly after start's day, at start's time of day."""
    ahead = (weekday - start.weekday()) % 7 or 7
    return start + timedelta(days=ahead)


def parse_proposed_time(text: str, now: datetime) -> Optional[datetime]:
    if not text or not text.strip():
        return None
    s = text.strip().lower()

    m = re.search(r"(\d{4}-\d{2}-\d{2})(?:[t\s]+(?:at\s+)?)(\d{1,2}):(\d{2})", s)
    if m:
        y, mo, d = (int(x) for x in m.group(1).split("-"))
        return _date(y, mo, d, int(m.group(2)), int(m.group(3)))

    m = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(20\d{2}))?"
        r"(?:\s*,\s*|\s+)(?:at\s+)?(?!20\d{2}\b)(\d{1,2}):?(\d{2})?\s*(am|pm)?\b", s,
    )
    if m:
        year = int(m.group(3)) if m.group(3) else now.year
        d = _date(year, _MONTHS[m.group(1)], int(m.group(2)), apply_am_pm(int(m.group(4)), m.group(6)) % 24, int(m.group(5) or 0))
        if d and not m.group(3) and d < now:
            d = _date(d.year + 1, d.month, d.day, d.hour, d.minute)
        return d

    # Lenient: callback windows ("2-4pm Monday", "tuesday 6-8pm", "2p.") resolve to the window's start.
    ns = re.sub(r"(\b\d{1,2})\s*p\.(?!m)", r"\1pm", s)
    ns = re.sub(r"(\b\d{1,2})\s*a\.(?!m)", r"\1am", ns)
    ns = re.sub(r"\b(\d{1,2})(?::\d{2})?\s*[-–]\s*\d{1,2}(?::\d{2})?\s*(am|pm)\b", r"\1\2", ns)
    day_m = re.search(rf"\b{_WEEKDAY}\b", ns)
    time_m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", ns)
    if day_m and time_m:
        d = _at(next_weekday(now, DAY_NAMES.index(day_m.group(1))), apply_am_pm(int(time_m.group(1)), time_m.group(3)), int(time_m.group(2) or 0))
        return d + timedelta(days=7) if d and d <= now else d
    tod = re.search(rf"\b{_WEEKDAY}\s+(morning|afternoon|evening|night)\b", ns)
    if tod:
        hour = {"morning": 10, "afternoon": 14, "evening": 18}.get(tod.group(2), 19)
        d = _at(next_weekday(now, DAY_NAMES.index(tod.group(1))), hour, 0)
        return d + timedelta(days=7) if d and d <= now else d

    m = re.search(rf"{_WEEKDAY}\s*(?:at\s*)?(\d{{1,2}}):?(\d{{2}})?\s*(am|pm)?", s)
    if m:
        d = _at(next_weekday(now, DAY_NAMES.index(m.group(1))), apply_am_pm(int(m.group(2)), m.group(4)), int(m.group(3) or 0))
        return d + timedelta(days=7) if d and d <= now else d

    m = re.search(r"tomorrow\s*(?:at\s*)?(\d{1,2}):?(\d{2})?\s*(am|pm)?", s)
    if m:
        return _at(now + timedelta(days=1), apply_am_pm(int(m.group(1)), m.group(3)), int(m.group(2) or 0))

    m = re.search(r"today\s*(?:at\s*)?(\d{1,2}):?(\d{2})?\s*(am|pm)?", s)
    if m:
        return _at(now, apply_am_pm(int(m.group(1)), m.group(3)), int(m.group(2) or 0))

    if s == "noon":
        d = _at(now, 12, 0)
        return d + timedelta(days=1) if d and d <= now else d

    m = re.search(
        r"(?:tonight|this evening)\s*(?:at\s*)?(\d{1,2}):?(\d{2})?\s*(am|pm)?|(?:at\s*)?(\d{1,2}):?(\d{2})?\s*(am|pm)\s*(?:tonight|this evening)", s
    )
    if m:
        hour = int(m.group(1) or m.group(4))
        return _at(now, apply_am_pm(hour, m.group(3) or m.group(6) or "pm"), int(m.group(2) or m.group(5) or 0))

    if re.search(r"\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", s):
        return None
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})?\s*(am|pm)?", s)
    if m:
        d = _at(now, apply_am_pm(int(m.group(1)), m.group(3)), int(m.group(2) or 0))
        return d + timedelta(days=1) if d and d <= now else d

    m = re.search(r"(\d{1,2})/(\d{1,2})\s*(?:at\s*)?(\d{1,2}):?(\d{2})?\s*(am|pm)?", s)
    if m:
        d = _date(now.year, int(m.group(1)), int(m.group(2)), apply_am_pm(int(m.group(3)), m.group(5)) % 24, int(m.group(4) or 0))
        if d and d < now:
            d = _date(d.year + 1, d.month, d.day, d.hour, d.minute)
        return d
    return None


def parse_date_reference(text: str, now: datetime) -> Optional[datetime]:
    s = str(text or "").strip().lower()
    if not s:
        return None
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if re.search(r"\btoday\b", s):
        return today
    if re.search(r"\btomorrow\b", s):
        return today + timedelta(days=1)
    m = re.search(rf"\b{_WEEKDAY}\b", s)
    if m:
        return next_weekday(today, DAY_NAMES.index(m.group(1)))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s)
    if m:
        year = int(m.group(3)) if m.group(3) else now.year
        year += 2000 if year < 100 else 0
        d = _date(year, int(m.group(1)), int(m.group(2)))
        if d and d < today:
            d = _date(d.year + 1, d.month, d.day)
        return d
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\s+(\d{1,2})\b", s)
    if m:
        d = _date(now.year, _MONTHS[m.group(1)], int(m.group(2)))
        if d and d < today:
            d = _date(d.year + 1, d.month, d.day)
        return d
    return None


def _availability(reference: str, now: datetime, hours: dict[str, Any]) -> dict[str, Any]:
    requested = parse_date_reference(reference, now)
    if requested is None:
        return {
            "is_valid": False, "reason": "could_not_parse", "availability_request": True,
            "message": "Could not understand the requested availability date. Ask the customer what day they have in "
            "mind. Do NOT propose any slot.",
            "next_available": None,
        }
    display = _format_date_reference(requested, now)
    closed = _day_hours(hours, requested) is None
    same_day = requested.date() == now.date()
    start = (now + MIN_LEAD_TIME).replace(second=0, microsecond=0) if same_day else requested
    slot = next_available(start, hours, now)
    return {
        "is_valid": False, "reason": "availability", "availability_request": True,
        "requested_display": display, "requested_day_closed": closed,
        "message": f"The dealership is closed {display}. Offer {slot}." if closed
        else f"The customer asked what times are available {display}. Offer {slot}.",
        "next_available": slot,
    }


def _day_hours(hours: dict[str, Any], day: datetime) -> Optional[tuple[int, int]]:
    """(open, close) in minutes for that day, None when closed or not set."""
    entry = hours.get(DAY_NAMES[day.weekday()]) if isinstance(hours, dict) else None
    if not isinstance(entry, dict) or not entry.get("open") or not entry.get("close"):
        return None
    try:
        return _minutes(entry["open"]), _minutes(entry["close"])
    except (ValueError, TypeError):
        return None


def _minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def next_available(start: datetime, hours: dict[str, Any], reference: Optional[datetime] = None) -> str:
    """The first opening at or after `start` within two weeks, as the customer would read it."""
    reference = reference or start
    for i in range(14):
        day = start + timedelta(days=i)
        span = _day_hours(hours, day)
        if span is None:
            continue
        candidate = day.replace(hour=span[0] // 60, minute=span[0] % 60, second=0, microsecond=0)
        if candidate > start:
            return format_for_display(candidate, reference)
        if i == 0:
            next_hour = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            if next_hour.date() == day.date() and next_hour.hour * 60 + next_hour.minute < span[1] and next_hour > start:
                return format_for_display(next_hour, reference)
    return "the next business day"


def _clock(d: datetime) -> str:
    hour12 = d.hour - 12 if d.hour > 12 else (12 if d.hour == 0 else d.hour)
    suffix = "PM" if d.hour >= 12 else "AM"
    return f"{hour12}{suffix}" if d.minute == 0 else f"{hour12}:{d.minute:02d}{suffix}"


def _format_12h(minutes: int) -> str:
    return _clock(datetime(2000, 1, 1, minutes // 60 % 24, minutes % 60))


def _day_label(d: datetime) -> str:
    return calendar.day_name[d.weekday()]


def format_for_display(d: datetime, reference: Optional[datetime] = None) -> str:
    if reference is not None:
        diff = (d.date() - reference.date()).days
        if diff == 0:
            return f"today at {_clock(d)}"
        if diff == 1:
            return f"tomorrow at {_clock(d)}"
    return f"{_day_label(d)}, {_MONTH_ABBR[d.month - 1]} {d.day} at {_clock(d)}"


def _format_date_reference(d: datetime, reference: datetime) -> str:
    diff = (d.date() - reference.date()).days
    if diff == 0:
        return "today"
    if diff == 1:
        return "tomorrow"
    return f"{_day_label(d)}, {_MONTH_ABBR[d.month - 1]} {d.day}"


def to_utc_iso(wall: datetime, tz_name: str) -> str:
    """The dealership wall-clock time as an absolute UTC instant, e.g. 2026-06-26T17:00:00.000Z."""
    return wall.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


VALIDATE_APPOINTMENT = Tool(
    name="Validate_Appointment",
    kind="code",
    description="Validate if a specific appointment time is within business hours and in the future, or return the "
    "next available slot for a date-only availability request. REQUIRED: Call this tool BEFORE you commit to any "
    "day+time in your reply. Input: proposed_time (string, e.g. 'Tuesday at 2:30pm') or availability_date (string, "
    "e.g. 'tomorrow', 'Monday', '2026-05-04').",
    parameters=schema({
        "proposed_time": {"type": "string", "description": "Specific appointment day and time to validate, such as 'Tuesday at 2:30pm'."},
        "availability_date": {"type": "string", "description": "Date-only availability request normalized by the model, such as 'today', 'tomorrow', 'Monday', or '2026-05-04'."},
        "availability_request": {"type": "boolean", "description": "True when the customer is asking what times are available on a date without proposing a specific time."},
    }),
    run=validate_appointment,
)


# ---------------------------------------------------------------------------------------------------------------
# Save_Appointment_Notes
# ---------------------------------------------------------------------------------------------------------------

_UPSERT_NOTE_SQL = """
INSERT INTO public.appointment_notes (lead_id, cadence_id, tenant_id, note)
VALUES ($1, $2, $3, $4)
ON CONFLICT (lead_id, cadence_id) DO UPDATE SET note = EXCLUDED.note, tenant_id = EXCLUDED.tenant_id, updated_at = now()
RETURNING id, lead_id, cadence_id, tenant_id, note, appointment_datetime, status, booking_message_id, updated_at
"""

# A booking (re)confirms the row: a previous cancellation is cleared with it, as the status/cancelled_at check needs.
_UPSERT_BOOKING_SQL = """
INSERT INTO public.appointment_notes (lead_id, cadence_id, tenant_id, note, appointment_datetime, booking_message_id, status)
VALUES ($1, $2, $3, $4, $5, $6, 'confirmed')
ON CONFLICT (lead_id, cadence_id) DO UPDATE SET
    note = EXCLUDED.note, tenant_id = EXCLUDED.tenant_id, appointment_datetime = EXCLUDED.appointment_datetime,
    booking_message_id = EXCLUDED.booking_message_id, status = 'confirmed',
    cancelled_at = NULL, cancellation_source = NULL, cancellation_reason = NULL, updated_at = now()
RETURNING id, lead_id, cadence_id, tenant_id, note, appointment_datetime, status, booking_message_id, updated_at
"""


async def save_appointment_notes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    note = str(args.get("note") or "").strip()
    if not note:
        return {"statusCode": 400, "body": {"message": "note is required"}}
    lead = await ctx.lead()
    cadence = await ctx.active_cadence()
    cadence_id = cadence.get("cadence_id") or lead.get("current_cadence_id")
    when = args.get("appointment_datetime")
    when = None if when in (None, "", "null") else str(when).strip()

    if when is None:
        # A note-only merge never erases an existing appointment or revives a cancellation.
        payload = {"lead_id": lead["id"], "cadence_id": cadence_id, "tenant_id": lead["tenant_id"], "note": note}
        if not ctx.live:
            return {"statusCode": 200, "body": [payload], "dry_run": True}
        row = await ctx.need_pool().fetchrow(_UPSERT_NOTE_SQL, lead["id"], cadence_id, lead["tenant_id"], note)
        return {"statusCode": 200, "body": [dict(row)]}

    if when not in ctx.validated_appointments:
        return {
            "statusCode": 422,
            "body": {"message": "appointment_datetime was not confirmed by Validate_Appointment in this turn. Nothing "
                     "was saved. Validate the exact day and time first and pass its confirmed_datetime verbatim; do "
                     "not claim the appointment is set."},
        }
    try:
        at = datetime.strptime(when, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"statusCode": 400, "body": {"message": f"appointment_datetime is not an ISO-8601 UTC time: {when}"}}
    inbound = await ctx.saved_inbound()
    booking_message_id = inbound["id"] if inbound and str(inbound.get("lead_id")) == str(lead["id"]) else None
    payload = {
        "lead_id": lead["id"], "cadence_id": cadence_id, "tenant_id": lead["tenant_id"], "note": note,
        "appointment_datetime": when, "booking_message_id": booking_message_id, "status": "confirmed",
    }
    if not ctx.live:
        return {"statusCode": 201, "body": [payload], "dry_run": True}
    row = await ctx.need_pool().fetchrow(
        _UPSERT_BOOKING_SQL, lead["id"], cadence_id, lead["tenant_id"], note, at, booking_message_id
    )
    return {"statusCode": 201, "body": [dict(row)]}


SAVE_APPOINTMENT_NOTES = Tool(
    name="Save_Appointment_Notes",
    kind="http",
    description="Save a validated appointment note to the database. REQUIRED: call only after Validate_Appointment "
    "returned is_valid=true for the exact day+time, and before writing any confirmation reply. Parameters: note "
    "(summary of conversation including validated appointment date/time, mileage, loan info, timeline, interest "
    "level), appointment_datetime (validated ISO 8601 date/time, or null only when no specific appointment time was "
    "validated). Response is { statusCode, body }: statusCode 200 or 201 with the saved row in body means SAVED. Any "
    "other statusCode, or an error body, means the save FAILED and nothing was recorded; do not claim the "
    "appointment is set.",
    parameters=schema({
        "note": {"type": "string", "description": "A detailed summary including: validated appointment date/time if scheduled, mileage confirmed, loan/payoff information, vehicle location, timeline for selling, and customer interest level. Do not describe an appointment as confirmed unless Validate_Appointment returned is_valid=true this turn."},
        "appointment_datetime": {"type": ["string", "null"], "description": "Copy the confirmed_datetime field returned by Validate_Appointment THIS TURN verbatim - a full ISO-8601 UTC timestamp like 2026-06-26T17:00:00.000Z. Do NOT reformat it, strip the Z/timezone, or convert the hours yourself. Return null if no specific appointment time was validated."},
    }, ["note"]),
    run=save_appointment_notes,
    needs=(NEEDS_CUSTOMER, NEEDS_DB),
    writes=True,
)


# ---------------------------------------------------------------------------------------------------------------
# Cancel_Appointment
# ---------------------------------------------------------------------------------------------------------------

_CANCEL_SQL = "SELECT public.cancel_appointment($1, $2, 'customer_sms', $3, $4)"
_CONFIRMED_SQL = """
SELECT id, appointment_datetime FROM public.appointment_notes
WHERE lead_id = $1 AND tenant_id = $2 AND status = 'confirmed' ORDER BY updated_at DESC
"""
_CANCEL_FAILED = (
    'The cancellation could not be recorded. Do NOT say anything was cancelled or cleared; call Escalate_To_Human('
    'reason="appointment cancellation could not be recorded", category=other, urgency=high) and say only that the '
    "team will confirm the cancellation."
)


async def cancel_appointment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason") or args.get("query") or "").strip()
    try:
        lead = await ctx.lead()
    except ToolFailure as exc:
        return {"ok": False, "cancelled": False, "error": str(exc), "message": _CANCEL_FAILED}
    pool = ctx.need_pool()
    if ctx.live:
        raw = await pool.fetchval(_CANCEL_SQL, lead["id"], lead["tenant_id"], reason[:500], ctx.inbound_text[:2000])
        r = json.loads(raw) if isinstance(raw, str) else (raw or {})
    else:
        rows = await pool.fetch(_CONFIRMED_SQL, lead["id"], lead["tenant_id"])
        r = {"ok": True, "cancelled": bool(rows), "cancelled_count": len(rows),
             "appointment_ids": [str(x["id"]) for x in rows] or None,
             "appointment_datetime": rows[0]["appointment_datetime"].isoformat() if rows and rows[0]["appointment_datetime"] else None}
        if not rows:
            r["reason"] = "no_confirmed_appointment"
    if r.get("ok") is True and r.get("cancelled") is True:
        out = {
            "ok": True, "cancelled": True, "cancelled_count": int(r.get("cancelled_count") or 1),
            "appointment_ids": r.get("appointment_ids"), "appointment_id": r.get("appointment_id"),
            "appointment_datetime": r.get("appointment_datetime"), "cancelled_at": r.get("cancelled_at"),
            "message": "Appointment cancelled and the dealer team paged. Tell the customer it is cancelled in one "
            "sentence; do not re-pitch and do not offer a new slot unless they ask.",
        }
    elif r.get("ok") is True:
        out = {
            "ok": True, "cancelled": False, "reason": r.get("reason") or "no_confirmed_appointment",
            "message": "No confirmed appointment on file. Say you do not see a booked appointment on your end and ask "
            "if they meant something else; do NOT say anything was cancelled or cleared.",
        }
    else:
        return {"ok": False, "cancelled": False, "reason": r.get("reason") or "rpc_not_ok", "message": _CANCEL_FAILED}
    if not ctx.live:
        out["dry_run"] = True
    return out


CANCEL_APPOINTMENT = Tool(
    name="Cancel_Appointment",
    kind="code",
    description="Cancel the customer's booked appraisal appointment. Call when the customer cancels or withdraws "
    "from a booked visit (cancel my appointment, I'm not going to come in, can't make it in, no longer coming). "
    "Records the cancellation on every confirmed appointment note for this customer and pages the dealer team. "
    "Returns cancelled=true only when a confirmed appointment was on file and is now cancelled; cancelled=false means "
    "nothing was on file. Never say an appointment is cancelled/cleared unless this returned cancelled=true THIS "
    "turn. A cancellation that names a NEW day/time is a reschedule: use Validate_Appointment + "
    "Save_Appointment_Notes instead. A price objection ('I'm not coming in for that price') is NOT a cancellation.",
    parameters=schema({"reason": {"type": "string", "description": "the customer's own words for the cancellation"}}, ["reason"]),
    run=cancel_appointment,
    needs=(NEEDS_CUSTOMER, NEEDS_DB),
    writes=True,
)

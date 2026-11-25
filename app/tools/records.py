"""
Tools over the customer's records and the dealership's knowledge base.

* Knowledge_Base_Search (n8n HTTP tool -> the query-kb webhook): a keyword search of the static knowledge base
  entries every agent already gets in its knowledge_base_system block (see app/knowledge). No database.
* Get_SMS_conversation_log (n8n Supabase tool): the thread's sms_log rows.
* Update_mileage (n8n Supabase tool): writes lead_vehicles.mileage. As in n8n, the figure must be one the customer
  stated in their latest message: the model's own number is never written.
"""
import re
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from app.knowledge.base import entries, search
from app.tools.base import NEEDS_CUSTOMER, NEEDS_DB, NEEDS_SMS, Tool, ToolContext, schema

# ---------------------------------------------------------------------------------------------------------------
# Knowledge_Base_Search
# ---------------------------------------------------------------------------------------------------------------


async def knowledge_base_search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    question = str(args.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "question is required"}
    found = search(entries(), question, ctx.settings.knowledge_base_limit)
    if not found:
        return {
            "ok": True, "results": [],
            "message": "Nothing in the knowledge base answers this. Do not guess; say a team member will confirm.",
        }
    return {
        "ok": True,
        "results": [{"kind": d.kind, "tags": d.tags, "content": d.content, "score": score} for d, score in found],
    }


KNOWLEDGE_BASE_SEARCH = Tool(
    name="Knowledge_Base_Search",
    kind="http",
    description="Search the dealership knowledge base for information about trade-ins, policies, FAQs, and SMS "
    "templates. Use this when you need specific information about dealership procedures, policies, or when answering "
    "customer questions about the trade-in process.",
    parameters=schema(
        {"question": {"type": "string", "description": "The customer's question to search in the knowledge base"}},
        ["question"],
    ),
    run=knowledge_base_search,
)

# ---------------------------------------------------------------------------------------------------------------
# Get_SMS_conversation_log
# ---------------------------------------------------------------------------------------------------------------


async def get_sms_conversation_log(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = max(1, min(int(args.get("limit") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    rows = await ctx.recent_sms(limit)
    return {
        "ok": True,
        "count": len(rows),
        "messages": [
            {
                "id": r["id"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "direction": r["direction"],
                "from": "customer" if r["direction"] == "inbound" else (r["sent_by"] or "dealership"),
                "campaign_message": r["cadence_id"] is not None and r["direction"] != "inbound",
                "message": r["message"],
            }
            for r in rows
        ],
    }


GET_SMS_CONVERSATION_LOG = Tool(
    name="Get_SMS_conversation_log",
    kind="db",
    description="Get this customer's SMS conversation with the dealership from the message log (sms_log), oldest "
    "first: every inbound and outbound text, including campaign messages. Use it when you need something said "
    "earlier in the thread that is not in the conversation you were given.",
    parameters=schema({"limit": {"type": "integer", "description": "Newest messages to return (default 50, at most 200)."}}),
    run=get_sms_conversation_log,
    needs=(NEEDS_SMS, NEEDS_DB),
)

# ---------------------------------------------------------------------------------------------------------------
# Update_mileage
# ---------------------------------------------------------------------------------------------------------------

_UPDATE_MILEAGE_SQL = """
UPDATE public.lead_vehicles SET mileage = $1, mileage_update_date = $2
WHERE id = $3 AND lead_id = $4
RETURNING id, mileage, mileage_update_date
"""

_MAKES = (
    "acura|audi|bmw|buick|cadillac|chevrolet|chevy|chrysler|dodge|ford|genesis|gmc|honda|hyundai|infiniti|jaguar|jeep|"
    "kia|land rover|lexus|lincoln|mazda|mercedes|mini|mitsubishi|nissan|porsche|ram|subaru|tesla|toyota|volkswagen|vw|"
    "volvo|wagoneer"
)
_MILEAGE_QUESTION = re.compile(r"\b(?:mileage|miles|odometer)\b", re.I)


def _scrub(text: str) -> str:
    """n8n tqMileageText: remove figures that are not the current odometer (tolerances, addresses, the mileage at
    purchase) and turn tenths into whole miles."""
    s = re.sub(r"\b(\d{1,3}),\s+(\d{3})\b", r"\1,\2", str(text or ""))

    def tenths(m: re.Match) -> str:
        before = m.string[max(0, m.start() - 3): m.start()]
        if re.search(r"[$£€]\s*$", before):
            return m.group(0)
        return str(round(int(m.group(1).replace(",", "")) + int(m.group(2)) / 10))

    s = re.sub(r"\b(\d{1,3}(?:,\d{3})+|\d{4,6})\.(\d)(?!\d)", tenths, s)
    s = re.sub(
        r"\b(?:give\s+or\s+take|plus\s+or\s+minus|within|less\s+than)\s+(?:(?:less\s+than|about|roughly)\s+)?\d[\d,]*"
        r"(?:\s*(?:miles?|mi))?\b|\+/-\s*\d[\d,]*(?:\s*miles?)?", " <TOLERANCE> ", s, flags=re.I,
    )
    s = re.sub(r"\b(?:located|address(?:\s+is)?|street\s+number)\s*(?:at\s*)?\d[\d,]*\b", " <ADDRESS> ", s, flags=re.I)
    s = re.sub(r"\b\d{2,6}\s+[a-z]+\s+(?:street|st|road|rd|avenue|ave|boulevard|blvd|drive|dr|lane|ln)\b", " <ADDRESS> ", s, flags=re.I)
    s = re.sub(r"\b(?:bought|purchased)\b[^.!?\n]{0,50}?\b(?:with|at)\s+\d[\d,]*(?:\s*k)?\s*(?:miles?|mi)\b", " <HISTORICAL> ", s, flags=re.I)
    s = re.sub(r"\b\d[\d,]*(?:\s*k)?\s*(?:miles?|mi)\s+(?:on\s+it\s+)?when\s+(?:i|we)\s+(?:bought|purchased)\b", " <HISTORICAL> ", s, flags=re.I)
    return s


def _foreign_vehicle(text: str, vehicle: dict[str, Any]) -> bool:
    """The figure is for another car: another make or VIN carries the reading."""
    t = text.lower()
    canon = lambda v: re.sub(r"[^a-z0-9]", "", str(v or "").lower().replace("chevy", "chevrolet").replace("vw", "volkswagen"))  # noqa: E731
    vin = canon(vehicle.get("vin"))
    if vin and any(canon(v) != vin for v in re.findall(r"\b[a-hj-npr-z0-9]{17}\b", t)):
        return True
    on_file = canon(vehicle.get("make"))
    for m in re.finditer(rf"\b(?:the|my|our|a|an)\s+(?:(?:19|20)\d{{2}}\s+)?({_MAKES})\b([^.!?;\n]{{0,90}})", t):
        if canon(m.group(1)) == on_file:
            continue
        rest = m.group(2)
        if re.match(r"^\s*(?:has|have|is\s+at|with|shows?)\s+\d[\d, .]*\s*(?:k|miles?|mi)\b", rest) or re.match(
            r"^\s+\w[\w -]{0,28}\s+(?:has|is\s+at|with)\s+\d[\d, .]*\s*(?:k|miles?|mi)\b", rest
        ):
            return True
    return False


def stated_mileage(text: str, last_outbound: str = "") -> tuple[Optional[int], str]:
    """(miles, "") when the customer's message states one odometer reading, else (None, why not)."""
    s = _scrub(text)
    readings: list[int] = []
    for m in re.finditer(r"(?<![\d$.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k\b|thousand\b)?\s*(miles?|mi\b)?", s, re.I):
        number, thousands, unit = m.group(1), m.group(2), m.group(3)
        value = float(number.replace(",", ""))
        if thousands:
            value *= 1000
        elif not unit:
            # A bare number counts when it is clearly an odometer: comma grouped or 4-6 digits that is not a year,
            # and the customer was asked about mileage or the message is little more than the number.
            digits = number.replace(",", "")
            if "." in number or not (4 <= len(digits) <= 6) or re.fullmatch(r"(?:19|20)\d{2}", digits):
                continue
            if not (_MILEAGE_QUESTION.search(last_outbound) or len(s.split()) <= 6):
                continue
        miles = int(round(value))
        if 1 <= miles <= 999_999:
            readings.append(miles)
    distinct = sorted(set(readings))
    if not distinct:
        return None, "no_mileage_in_message"
    if len(distinct) > 1:
        return None, "conflicting_mileage_figures"
    return distinct[0], ""


async def update_mileage(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    miles, why = stated_mileage(ctx.combined_message or ctx.inbound_text, ctx.last_outbound)
    if miles is None:
        return {"ok": False, "error": "mileage_not_stated_by_customer", "reason": why,
                "message": "Nothing was written. Only an odometer reading the customer states in their latest message "
                "is saved; ask them for the current mileage if you need it."}
    requested = args.get("mileage")
    if requested not in (None, ""):
        try:
            if int(float(str(requested).replace(",", ""))) != miles:
                return {"ok": False, "error": "mileage_differs_from_customer_message", "customer_stated": miles,
                        "message": "Nothing was written: the figure passed is not the one the customer stated."}
        except ValueError:
            pass
    vehicle = await ctx.vehicle()
    if not vehicle or not vehicle.get("id"):
        return {"ok": False, "error": "no_vehicle_on_file", "message": "Nothing was written: no vehicle on file for this customer."}
    if _foreign_vehicle(ctx.inbound_text, vehicle):
        return {"ok": False, "error": "mileage_for_another_vehicle",
                "message": "Nothing was written: the mileage seems to be for a different vehicle than the one on file."}
    lead = await ctx.lead()
    today = ctx.now_utc.astimezone(ZoneInfo(await ctx.timezone_name())).date()
    vehicle_label = " ".join(str(vehicle.get(k)) for k in ("year", "make", "model") if vehicle.get(k))
    if not ctx.live:
        return {"ok": True, "dry_run": True, "vehicle_id": str(vehicle["id"]), "vehicle": vehicle_label,
                "mileage": miles, "mileage_update_date": today.isoformat()}
    # resolve_thread_vehicle returns JSON, so its id is text.
    vehicle_id = UUID(str(vehicle["id"]))
    row = await ctx.need_pool().fetchrow(_UPDATE_MILEAGE_SQL, miles, today, vehicle_id, lead["id"])
    if row is None:
        return {"ok": False, "error": "vehicle_not_updated", "message": "Nothing was written: the vehicle row was not found."}
    return {"ok": True, "vehicle_id": str(row["id"]), "vehicle": vehicle_label, "mileage": row["mileage"],
            "mileage_update_date": row["mileage_update_date"].isoformat()}


UPDATE_MILEAGE = Tool(
    name="Update_mileage",
    kind="db",
    description="Save the current odometer reading of the customer's vehicle (lead_vehicles.mileage, with today's "
    "date). Call it when the customer states their mileage. Only the figure the customer wrote in their latest "
    "message is saved; if no figure is there, or the message gives conflicting figures, nothing is written.",
    parameters=schema({"mileage": {"type": "integer", "description": "The mileage exactly as the customer stated it in their latest message, in miles (e.g. 45000 for '45k')."}}),
    run=update_mileage,
    needs=(NEEDS_CUSTOMER, NEEDS_DB),
    writes=True,
)

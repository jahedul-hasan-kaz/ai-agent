"""
Tools that hand a conversation to people or stop contact.

* Escalate_To_Human (n8n Code tool): inserts an `escalations` row; a Postgres trigger then alerts the dealer and
  pauses the cadence. The n8n text gates are kept: a deferral ("let me talk to my wife"), a bare acknowledgment
  ("ok thanks") or a passive "I'll wait for the call" never pages anyone, and a repeated decline is sent to
  Suppress_Lead. The gates that read n8n's Intent Classifier (protected closes; the deceased / wrong-owner halt and
  hold) run when the caller supplies that node's output in node_data; without it only the text gates apply.
* Suppress_Lead (n8n Code tool): public.apply_lead_suppression. Model arguments are requests, not authorization:
  a permanent DNC needs a STOP-keyword turn or a reported death, and vehicle_sold needs a plain disposal statement,
  both read from the customer's own message here, not from the model.
"""
import json
import re
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.tools.base import NEEDS_CUSTOMER, NEEDS_DB, Tool, ToolContext, ToolFailure, schema

# ---------------------------------------------------------------------------------------------------------------
# Escalate_To_Human
# ---------------------------------------------------------------------------------------------------------------

CATEGORIES = ["relay_request", "missed_callback", "complaint", "refund", "value_request", "vehicle_unavailable", "service_complaint", "other"]
_ROLE_TOKENS = {"manager", "managers", "sales", "service", "finance", "parts", "team", "supervisor", "owner", "boss", "gm"}
_STOP_TOKENS = set(
    "to in at for with about from of on the a an my our your every chat check call text back now wife husband partner "
    "spouse family friend person people human ai bot robot someone somebody anyone anybody everyone everybody nobody "
    "them they you me him her us morning afternoon evening tonight today tomorrow time number auto generated tried "
    "would freed up this that it one".split()
)

_DEFERRAL_CHUNK = re.compile("^(?:" + "|".join([
    r"(?:i|we)(?:'| )?(?:ll|will|would|d|m)?",
    r"let me",
    r"have to|need to|got to|gotta|going to|gonna|probably|maybe|just|might|may",
    r"get back (?:to|with) (?:you|ya)(?: on (?:that|this)(?: one)?)?",
    r"let you know",
    r"think (?:about|on|it over)(?: it| this| that)?",
    r"sleep on it",
    r"(?:talk|speak|chat|check|discuss) (?:it over |this |that )?(?:to|with) (?:my|our|the) (?:wife|husband|partner|spouse|family|kids?|son|daughter|dad|mom|father|mother|brother|sister)",
    r"run it by (?:my|our) \w+",
    r"after (?:speaking|talking|checking) (?:to|with) (?:my|our) \w+",
    r"on (?:that|this)(?: one)?",
    r"for now|right now|at the moment|at this time|later|soon|eventually|at some point|down the road",
    r"next (?:week|month|year|time)",
    r"when (?:i|we) (?:bring|take|get) (?:my|the) \w+ in",
    r"schedule something|set something up|wait",
    r"to|and|but|so|then",
]) + r")\b\s*")
_CLOSING_SIGNAL_CHUNK = re.compile("^(?:" + "|".join([
    r"thanks|thank you|thank u|thank ya|thankyou|thanx|thx|tks|txs|thks|thnx|ty|much appreciated|many thanks",
    r"(?:i |we )?(?:really |truly |much )?appreciate(?: (?:it|that|you|the help|your help|the info(?:rmation)?|you all))?",
    r"(?:you|u) (?:too|two|to|as well)|same to you|(?:and )?you (?:do )?the same",
    r"you do the same|(?:you|u|your|youre|you re|ur) (?:are )?welcome",
    r"bye|goodbye|later|talk soon|talk to you (?:soon|later)|ttyl|cheers",
    r"(?:you )?(?:take care|be safe|stay safe|god bless|bless you)(?: (?:too|as well|also))?",
    r"(?:have|enjoy) (?:a|an|the|your) (?:\w+ ){0,3}(?:rest of (?:your|the) )?(?:\w+ ){0,2}(?:day|night|one|evening|morning|afternoon|weekend|week)",
]) + r")\b\s*")
_CLOSING_FILLER_CHUNK = re.compile("^(?:" + "|".join([
    r"ok(?:ay)?|okey|kk|alright(?:y)?|yes|yep|yup|ya|yeah|sure(?: thing)?|no problem|np",
    r"will do|wilco|can do",
    r"sounds? (?:good|great)|got it|good|great|perfect|awesome|cool|excellent|lol|haha",
    r"copy(?: that)?|roger(?: that)?|10 4|gotcha|understood|noted|bet|word",
    r"though|anyway|anyhow|again|so (?:much|very much)|very much|a lot|a bunch|kindly|much",
    r"for (?:the |your |all |all the )?(?:\w+ ){0,2}(?:reply|response|help|info(?:rmation)?|update|time|call|text|message|asking|reaching out|checking in|letting me know|everything)",
    r"and|also|too|as well|mr|mrs|ms",
    r"(?:my )?(?:friend|man|dude|bro(?:ther)?|buddy|pal|boss|chief|fam|sir|maam|ma am)",
]) + r")\b\s*")
_LOW_SIGNAL_EXTRA_CHUNK = re.compile(r"^(?:please|pls|plz|absolutely|definitely|of course)\b\s*")
_CONTACT_OFFER = re.compile(
    r"\b(?:have\s+(?:someone|somebody|the\s+team|a\s+(?:manager|buyer|specialist)|our\s+(?:team|managers?|buyers?))\b"
    r"[^.?!\n]{0,40}\b(?:call|phone|reach\s+out)\b|when\s+would\s+be\s+a\s+(?:convenient|good|best)\s+time\s+for\s+a\s+call"
    r"|i\s+can\s+have\s+someone\s+(?:call|reach\s+out)|give\s+you\s+a\s+call|quick\s+call\s+with\s+one\s+of\s+our\s+buyers"
    r"|(?:good|best|convenient)\s+time\s+(?:for\s+(?:a|the)\s+call|to\s+(?:call|reach)\s+you))\b", re.I,
)
_PASSIVE_CALLBACK = re.compile(
    r"^(?:thanks[,!. ]*)?(?:i(?: will|'ll) wait for (?:the|your) call|ok(?:ay)?[,!. ]*thanks|thank you)[.!\s]*$", re.I
)


def sanitize_named_person(raw: Any) -> str:
    """A real name or a dealership role, or '' (n8n named-person-shape-v1)."""
    s = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z' -]", " ", str(raw or ""))).strip()
    previous = None
    while s and s != previous:
        previous = s
        s = re.sub(r"^(?:to|in|at|for|with|about|from|of|on|the|a|an|my|our|your)\s+", "", s, flags=re.I)
    tokens = s.split()[:3]
    if not tokens:
        return ""
    if all(t.lower() in _ROLE_TOKENS for t in tokens):
        return " ".join(tokens)
    if any(t.lower() in _STOP_TOKENS for t in tokens):
        return ""
    if not any(re.fullmatch(r"[a-z][a-z'-]{1,29}", t, re.I) for t in tokens):
        return ""
    return " ".join(tokens)


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())).strip()


def is_deferral_only(text: str) -> bool:
    rest, saw, previous = _words(text), False, None
    if not rest:
        return False
    while rest and rest != previous:
        previous = rest
        if _DEFERRAL_CHUNK.match(rest):
            saw = True
            rest = _DEFERRAL_CHUNK.sub("", rest, count=1)
            continue
        rest = _CLOSING_FILLER_CHUNK.sub("", _CLOSING_SIGNAL_CHUNK.sub("", rest, count=1), count=1)
    return saw and rest == ""


def is_closing_filler_only(text: str) -> bool:
    if not str(text or "").strip():
        return False
    rest, previous = _words(text), None
    if not rest:
        return True  # a lone emoji or "!!" is a reaction, not a request
    while rest and rest != previous:
        previous = rest
        rest = _CLOSING_SIGNAL_CHUNK.sub("", rest, count=1)
        rest = _CLOSING_FILLER_CHUNK.sub("", rest, count=1)
        rest = _LOW_SIGNAL_EXTRA_CHUNK.sub("", rest, count=1)
    return rest == ""


def is_repeat_decline(text: str) -> bool:
    return bool(
        re.search(r"\b(?:already|repeatedly|again|told you)\b", text, re.I)
        and re.search(r"\b(?:not interested|no thanks|stop|don'?t want|do not want)\b", text, re.I)
    )


# n8n protectedCloseCategory: classifier intents that are never a protected close, and those that are complaint work.
_NOT_PROTECTED = {"low_signal", "vehicle_unavailable", "future_callback", "relay_request", "missed_callback",
                  "hardship_disclosed", "credit_denial", "deceased", "self_harm"}
_COMPLAINT_INTENTS = {"complaint", "service_complaint", "legal_threat"}


def protected_close_category(classifier: dict[str, Any], text: str) -> Optional[str]:
    """n8n protectedCloseCategory: the close that Suppress_Lead owns for this turn, from the Intent Classifier's
    output, or None. A supervisor request is complaint work even when it concerns unwanted messages."""
    c, intent = classifier, classifier.get("intent")
    if intent in _NOT_PROTECTED:
        return None
    if c.get("complaint_signals") is True or intent in _COMPLAINT_INTENTS:
        return None
    if c.get("internal_employee") is True or intent == "internal_employee":
        return "internal_employee"
    if c.get("wrong_owner") is True or intent == "wrong_owner":
        return "wrong_owner"
    # A requested capability with an explicitly cleared decline flag beats a raw repeated-call refusal.
    if c.get("explicit_decline_or_deferral") is False and (
        intent in ("value_request", "inventory", "scheduling") or c.get("active_buying_request") is True
        or c.get("active_finance_question") is True or c.get("active_appraisal_scheduling") is True
    ):
        return None
    if intent == "repeat_decline" or is_repeat_decline(text):
        return "repeat_decline"
    if c.get("explicit_decline_or_deferral") is True or intent in ("not_interested_soft", "polite_decline", "optin_decline"):
        return "polite_decline"
    return None


_MAX_BIGINT = 9223372036854775807


def _receipt_binds(saved: dict[str, Any], tenant_id: Any, lead_id: Any) -> bool:
    """n8n's sales-handoff receipt check: this dealership's and lead's inbound row with a positive bigint id."""
    raw = str(saved.get("id") or "")
    return (
        str(saved.get("tenant_id") or "") == str(tenant_id or "") and str(saved.get("lead_id") or "") == str(lead_id or "")
        and saved.get("direction") == "inbound" and re.fullmatch(r"[1-9]\d*", raw) is not None and int(raw) <= _MAX_BIGINT
    )


def _sales_handoff_id(inbound_id: int) -> str:
    """n8n's deterministic escalation id for one inbound message: a retry of the same turn never pages twice."""
    h = format(inbound_id, "016x")
    return f"a1080908-{h[:4]}-4{h[4:7]}-8{h[7:10]}-000000{h[10:]}"


_INSERT_ESCALATION_SQL = """
INSERT INTO public.escalations (id, tenant_id, lead_id, source, lane, origin, category, inbound_message, conversation_summary)
VALUES (coalesce($1::uuid, gen_random_uuid()), $2, $3, $4, $5, 'customer', $6, $7, $8)
ON CONFLICT (id) DO NOTHING
RETURNING id, category
"""
_EXISTING_ESCALATION_SQL = "SELECT id, category FROM public.escalations WHERE id = $1 AND tenant_id = $2 AND lead_id = $3"


async def _summary(ctx: ToolContext) -> str:
    """The last five messages, as the dealer sees them in the escalation."""
    tz = ZoneInfo(await ctx.timezone_name())
    try:
        rows = await ctx.recent_sms(5)
    except Exception:  # noqa: BLE001 - the summary is a courtesy; the escalation must still go in
        rows = []
    lines = []
    if rows:
        for r in rows:
            at = r.get("created_at")
            if isinstance(at, str):  # a supplied "Get Recent SMS History" row
                try:
                    at = datetime.fromisoformat(at.replace("Z", "+00:00"))
                except ValueError:
                    at = None
            stamp = at.astimezone(tz).strftime("%m/%d/%Y, %I:%M %p").replace(", 0", ", ") if isinstance(at, datetime) else "date/time unavailable"
            lines.append(f"[{stamp}] {'Customer' if r.get('direction') == 'inbound' else 'AI'}: {r.get('message') or ''}")
    else:
        for t in [t for t in ctx.turns if t["role"] in ("user", "assistant")][-5:]:
            lines.append(f"[date/time unavailable] {'Customer' if t['role'] == 'user' else 'AI'}: {t['content']}")
    return "\n".join(lines)


async def escalate_to_human(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason") or args.get("query") or "").strip()
    named_person = sanitize_named_person(args.get("named_person") or args.get("namedPerson"))
    urgency = str(args.get("urgency") or "normal").strip().lower()
    urgency = urgency if urgency in ("normal", "high", "urgent") else "normal"
    raw_category = str(args.get("category") or "").strip().lower()
    category = raw_category if raw_category in CATEGORIES else ("relay_request" if named_person else "other")
    legacy_dnc = bool(args.get("permanent_dnc") or args.get("permanentDnc"))
    inbound = ctx.inbound_text
    turn_text = ctx.combined_message or inbound
    classifier = ctx.classifier()

    if _PASSIVE_CALLBACK.match(turn_text.strip()):
        return {"ok": False, "error": "passive_callback_continuation_no_new_page",
                "message": "This is only an acknowledgment of a prior callback. Do not page again or claim new delivery."}
    protected = (
        protected_close_category(classifier, turn_text) if classifier is not None
        # Without the classifier only its text rule applies; a complaint category stands in for complaint_signals.
        else "repeat_decline" if category not in ("complaint", "service_complaint") and is_repeat_decline(turn_text) else None
    )
    if protected:
        return {"ok": False, "error": "protected_close_uses_suppression",
                "message": "Use Suppress_Lead for this protected close. Do not promise a dealer page."}
    if not reason:
        return {"ok": False, "error": "reason is required"}
    if not named_person and is_deferral_only(inbound):
        return {"ok": False, "blocked": "deferral_no_escalation",
                "message": "The customer is deferring, not asking to reach a person. Do not escalate and do not claim "
                "the team was notified. Reply with one brief, pressure-free acknowledgment that they can reach out anytime."}
    if (not named_person and urgency != "urgent" and category not in ("complaint", "service_complaint", "refund")
            and is_closing_filler_only(inbound) and not _CONTACT_OFFER.search(ctx.last_outbound)):
        return {"ok": False, "blocked": "low_signal_no_escalation",
                "message": "The customer sent a brief acknowledgment, not a request to reach a person. Do not escalate "
                "and do not claim the team was notified. Answer the thread or close warmly."}

    lead = await ctx.lead()
    saved = await ctx.saved_inbound()
    sales = category in ("other", "relay_request", "value_request")
    if sales and saved and not _receipt_binds(saved, lead["tenant_id"], lead["id"]):
        return {"ok": False, "error": "missing_current_saved_inbound",
                "message": "No follow-up was verified. Ask the customer to contact the dealership directly."}
    handoff_id = _sales_handoff_id(int(saved["id"])) if sales and saved else None
    summary = (
        f"[AI escalation] reason={reason}; category={category}; named_person={named_person or 'n/a'}; "
        f"urgency={urgency}\n\nLast 5 messages:\n{await _summary(ctx)}"
    )
    row = {
        "id": handoff_id, "tenant_id": lead["tenant_id"], "lead_id": lead["id"], "source": ctx.source,
        "lane": "service" if category == "service_complaint" else "sales", "category": category,
        "inbound_message": inbound, "conversation_summary": summary,
    }
    result = {"ok": True, "permanent_dnc": False, "dnc_applied": False, "category": category, "urgency": urgency,
              "named_person": named_person or None}
    if handoff_id:
        result.update({"tenant_id": str(lead["tenant_id"]), "lead_id": str(lead["id"]), "inbound_log_id": str(saved["id"]),
                       "action": "sales_handoff", "dealer_delivery_verified": False})
    if not ctx.live:
        suppression = await _classifier_suppression(ctx, classifier, lead, reason, legacy_dnc)
        return {**result, "suppression": suppression, "message": _escalation_message(suppression), "escalation_id": None,
                "dry_run": True, "would_insert": {k: v for k, v in row.items() if k != "conversation_summary"}}

    pool = ctx.need_pool()
    inserted = await pool.fetchrow(
        _INSERT_ESCALATION_SQL, handoff_id, row["tenant_id"], row["lead_id"], row["source"], row["lane"], category,
        inbound, summary,
    )
    if inserted is None and handoff_id:  # this turn was already escalated: report that row, never a second page
        inserted = await pool.fetchrow(_EXISTING_ESCALATION_SQL, handoff_id, row["tenant_id"], row["lead_id"])
    if inserted is None:
        raise ToolFailure("the escalation could not be saved")
    suppression = await _classifier_suppression(ctx, classifier, lead, reason, legacy_dnc)
    return {**result, "suppression": suppression, "message": _escalation_message(suppression),
            "escalation_id": str(inserted["id"]), "category": inserted["category"]}


async def _classifier_suppression(
    ctx: ToolContext, classifier: Optional[dict[str, Any]], lead: dict[str, Any], reason: str, legacy_dnc: bool
) -> dict[str, Any]:
    """n8n permanent-dnc-halt-hold-v1: when the Intent Classifier detected a deceased owner or a disputed ownership,
    the escalation also halts cadences and holds sends (never a DNC). Evidence is the classifier's, never the
    model's; without a supplied classifier nothing is written."""
    c = classifier or {}
    category = "deceased" if c.get("deceased") is True else (
        "wrong_owner" if c.get("wrong_owner") is True or c.get("intent") == "wrong_owner" else None)
    out: dict[str, Any] = {
        "schema_version": "escalate_to_human_suppression.v1", "legacy_permanent_dnc_requested": legacy_dnc,
        "legacy_permanent_dnc_honored": False, "category": category, "attempted": False, "ok": False,
        "permanent_dnc": False, "dnc_applied": False, "cadences_halted": 0, "hold_applied": False,
        "suppression_effective": False, "nothing_written": True, "error": None, "result": None,
    }
    if not category:
        return out
    out["attempted"] = True
    reason_text = f"escalate_to_human:{category}: {reason}"[:500]
    if ctx.live:
        try:
            raw = await ctx.need_pool().fetchval(_APPLY_SUPPRESSION_SQL, lead["id"], lead["tenant_id"], reason_text, False, False, category)
            r = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as exc:  # noqa: BLE001 - the escalation is saved; report the suppression failure
            out["error"] = f"{type(exc).__name__}: {exc}"[:200] or "apply_lead_suppression failed"
            return out
    else:  # what the RPC does with p_permanent_dnc=false for these categories: halt + hold
        r = {"success": True, "dry_run": True, "dnc_applied": False, "hold_applied": True, "cadences_halted": 1}
    r = r if isinstance(r, dict) else {}
    halted = r.get("cadences_halted") if isinstance(r.get("cadences_halted"), int) else 0
    out.update(result=r, ok=r.get("success") is True, dnc_applied=r.get("dnc_applied") is True,
               permanent_dnc=r.get("dnc_applied") is True, cadences_halted=halted, hold_applied=r.get("hold_applied") is True)
    out["suppression_effective"] = out["ok"] and (
        r.get("suppression_effective") is True or out["dnc_applied"] or halted > 0 or out["hold_applied"])
    out["nothing_written"] = not out["suppression_effective"]
    return out


def _escalation_message(suppression: dict[str, Any]) -> str:
    """n8n's receipt message, with its note scoped to this tool's own suppression result."""
    base = "The request is saved for dealership review. Do not claim notification delivery or promise a call time."
    if not suppression["attempted"]:
        if suppression["legacy_permanent_dnc_requested"]:
            return base + (" permanent_dnc was IGNORED: this tool never DNCs a contact and wrote no suppression. Do NOT "
                           "claim the customer is off the list or that messages will stop on the strength of THIS result; "
                           "if suppression is warranted, call Suppress_Lead with its category and follow its receipt.")
        return base
    if suppression["suppression_effective"]:
        return base + (f" Active cadences were halted and a send hold is in place for this contact "
                       f"({suppression['category']}; no DNC row). Do NOT claim the customer was unsubscribed, removed or DNC'd.")
    error = f": {suppression['error']}" if suppression["error"] else ""
    note = (f" This tool wrote no suppression for this contact ({suppression['category']}{error}). Do NOT claim the "
            "customer is off the list or that messages will stop on the strength of THIS result; only a Suppress_Lead "
            "receipt with suppression_effective=true licenses that.")
    if suppression["category"] == "deceased":
        note += " Suppress_Lead(suppression_category=deceased) owns the deceased suppression."
    return base + note


ESCALATE_TO_HUMAN = Tool(
    name="Escalate_To_Human",
    kind="code",
    description="Escalate to a human at the dealership. Call when the customer asks to speak with a person, names a "
    "specific employee, requests a callback, expresses unresolved frustration, reports a missed callback, makes a "
    "complaint or threat, or wants immediate contact. Creates an escalation row; the Postgres trigger alerts the "
    "dealer and pauses cadence through the Escalation Notifier workflow. Do not claim the team was notified unless "
    "this tool returns ok=true. Always pass category (what the issue IS - relay_request ONLY when the customer asked "
    "to speak with or hear from someone) separately from urgency (how fast a human must act); they are independent. "
    "Use category=service_complaint when the issue concerns a service visit, repair, or the service department.",
    parameters=schema({
        "reason": {"type": "string", "description": "One-sentence summary of why a human should follow up, using the customer's words when possible."},
        "category": {"type": "string", "enum": CATEGORIES, "description": "WHAT the issue is (dealer queue label) - independent of urgency. relay_request ONLY when the customer asks to speak with/hear from a person, names someone, or asks for a call/callback. missed_callback when a promised call or follow-up never happened. complaint for dissatisfaction, hostility, threats, legal/media pressure. refund for refund/deposit/money-back. value_request when a human must provide or verify trade-in/offer numbers. vehicle_unavailable for follow-up about a vehicle that is gone or unavailable. other for everything else - use other when unsure, never relay_request. service_complaint for problems with a service visit, repair, or the service department (routes lane=service)."},
        "named_person": {"type": "string", "description": "Specific person or dealership role the customer asked for, such as Mike, manager, sales, service. Must be a real name or role the dealership can act on. Omit entirely when none; never fill it from mid-sentence words (to, in, my wife, an AI). The tool discards non-name values."},
        "urgency": {"type": "string", "enum": ["normal", "high", "urgent"], "description": "normal for routine relay, high for immediate contact or missed callback, urgent for threats/legal/safety/deceased-owner cases."},
    }, ["reason", "category", "urgency"]),
    run=escalate_to_human,
    needs=(NEEDS_CUSTOMER, NEEDS_DB),
    writes=True,
    # n8n Tool Context fields this tool reads (escalate_to_human.js).
    inputs=(
        ("Lookup Lead", "id"), ("Normalize Phone", "text"), ("Combine messages", "combinedMessage"),
        ("Get Recent SMS History", ""), ("Get tenant emails & phone", "hours_of_operation.timezone"),
        ("Get tenant emails & phone", "timezone"), ("Insert Log", "id"), ("Insert Log", "tenant_id"),
        ("Insert Log", "lead_id"), ("Insert Log", "direction"),
        ("Intent Classifier", "intent"), ("Intent Classifier", "complaint_signals"),
        ("Intent Classifier", "internal_employee"), ("Intent Classifier", "wrong_owner"),
        ("Intent Classifier", "deceased"), ("Intent Classifier", "explicit_decline_or_deferral"),
        ("Intent Classifier", "active_buying_request"), ("Intent Classifier", "active_finance_question"),
        ("Intent Classifier", "active_appraisal_scheduling"),
    ),
)

# ---------------------------------------------------------------------------------------------------------------
# Suppress_Lead
# ---------------------------------------------------------------------------------------------------------------

SUPPRESSION_CATEGORIES = ["lease_disclosed", "vehicle_sold", "wrong_owner", "deceased", "polite_decline", "repeat_decline", "internal_employee", "past_grievance"]
# A refused permanent DNC on these becomes a cadence halt plus a send hold (apply_lead_suppression does both).
_HALT_SET = {"internal_employee", "wrong_owner", "repeat_decline", "past_grievance"}
_STOP_KEYWORD = re.compile(r"^\s*(stop|stopall|stop all|unsubscribe|cancel|end|quit|opt[ -]?out)\W*$", re.I)
_DECEASED = re.compile(
    r"\b(?:passed\s+away|has\s+passed|have\s+passed|died|dead|deceased|no\s+longer\s+with\s+us|rest\s+in\s+peace|funeral|"
    r"lost\s+(?:my|our|her|his)\s+(?:husband|wife|father|mother|dad|mom|son|daughter|brother|sister))\b", re.I,
)
_DISPOSAL = re.compile(
    r"\b(?:sold|traded(?:\s+(?:it|her|him|the\s+\w+))?\s+in|traded|got\s+rid\s+of|totaled|totalled|wrecked|"
    r"(?:was|got)\s+(?:repossessed|repo'?d|stolen)|turned\s+(?:it|the\s+\w+)\s+in|returned\s+(?:the\s+)?lease|"
    r"(?:no\s+longer|don'?t|do\s+not)\s+(?:have|own)\s+(?:it|that|the|this|my))\b", re.I,
)


def _retained(text: str) -> bool:
    return bool(re.search(
        r"\b(?:still\s+(?:own|have|drive|driving|keep)|kept\s+(?:it|the\s+(?:car|vehicle))|(?:i|we)\s+bought\s+(?:it|the\s+(?:car|vehicle))\s+back)\b",
        text, re.I,
    ))


def disposal_vetoed(raw: str, vehicle: Optional[dict[str, Any]]) -> bool:
    """n8n disposalSemanticVeto: a hypothetical, a request to sell, or the disposal of another vehicle."""
    vehicle = vehicle or {}
    if _retained(raw):
        return True
    t = str(raw or "").replace("‘", "'").replace("’", "'").upper()
    hypothetical = re.search(
        r"\b(?:IF|SUPPOSE|SUPPOSING|IMAGINE|WISH|HOP(?:E|ING)|WOULD|COULD|MIGHT|MAY|WILL)\b(?!\s+(?:TELL|SAY|MENTION|EXPLAIN|ADMIT)\b)"
        r"[^.!?\n]{0,150}\b(?:SELL|SOLD|TRAD(?:E|ED|ING)|TOTAL(?:ED)?|CRASH(?:ED)?|WRECK(?:ED)?|BURN\w*|RETURN(?:ED)?|SURRENDER(?:ED)?)\b", t,
    )
    sale_request = re.search(r"\b(?:DOESN'?T|WON'?T|DOES NOT|WILL NOT)\s+RUN\b", t) and re.search(
        r"\b(?:SELL|TRADE|BUY|PICK\s+(?:IT\s+)?UP|COME\s+GET|COLLECT)\b", t
    )
    if hypothetical or sale_request:
        return True
    key = lambda x: re.sub(r"[^A-Z0-9]", "", str(x or "").upper())  # noqa: E731
    model = key(vehicle.get("model"))
    if not model:
        return False
    received = re.search(r"\bTRADED\b[^.!?\n]{0,80}?\bFOR\s+(.+?)(?:[.!?\n]|$)", t)
    if received and model in key(received.group(1)):
        return True
    disposed = re.search(
        r"\b(?:SOLD|TRADED|SURRENDERED|RETURNED|TOTAL+ED|WRECKED)\s+(?:IN\s+)?(?:MY|OUR|THE|A|AN)\s+(.+?)"
        r"(?=\s+(?:FOR|TO|AND|BUT|YESTERDAY|TODAY|LAST|AGO)\b|[.!?\n]|$)", t,
    )
    if not disposed or re.match(r"^(?:CAR|VEHICLE|TRUCK|SUV|VAN|IT|THIS|THAT|MINE|ONE)\b", disposed.group(1)):
        return False
    year = re.search(r"\b(?:19|20)\d{2}\b", disposed.group(1))
    return model not in key(disposed.group(1)) or bool(year and vehicle.get("year") and int(year.group(0)) != int(vehicle["year"]))


_APPLY_SUPPRESSION_SQL = "SELECT public.apply_lead_suppression($1, $2, $3, $4, $5, NULL, $6)"
_NO_STOP_NUDGE = {"lease_disclosed", "polite_decline"}


def _noop_message(category: str) -> str:
    if category == "deceased":
        return ("Nothing was suppressed: no DNC, no cadence halt, no send hold. Do NOT promise the contact will stop. "
                "Reply ONCE with empathy plus the honest step: \"I'm so sorry for your loss. Reply STOP and you won't "
                "receive further messages.\"")
    if category in _NO_STOP_NUDGE:
        return ("Nothing was suppressed: no DNC, no cadence halt, no send hold. Close warmly. Do NOT tell the customer "
                "they are off the list or that messages will stop, and do NOT mention STOP; they did not ask to stop.")
    if category == "vehicle_sold":
        return ("Nothing was written: the vehicle record was NOT updated. Do NOT say the vehicle was updated, that the "
                "customer is off the list, or that messages will stop.")
    return ("Nothing was suppressed: no DNC, no cadence halt, no send hold. Do NOT tell the customer they are off the "
            "list or that messages will stop. Reply: \"Got it, sorry for the bother. Reply STOP and you won't receive "
            "further messages.\"")


async def suppress_lead(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason") or args.get("query") or "").strip()
    category = str(args.get("suppression_category") or "").strip()
    permanent_dnc = bool(args.get("permanent_dnc"))
    vehicle_disposed = bool(args.get("vehicle_disposed"))
    if not reason:
        return {"ok": False, "error": "reason is required"}
    if not category:
        return {"ok": False, "error": "suppression_category is required"}
    if category not in SUPPRESSION_CATEGORIES:
        return {"ok": False, "error": f"invalid suppression_category: {category}"}
    is_sold = category == "vehicle_sold"
    if is_sold != vehicle_disposed or (is_sold and permanent_dnc):
        return {"ok": False, "error": "incoherent_suppression_mutation_tuple", "suppression_category": category,
                "permanent_dnc": False, "vehicle_disposed": False}

    text = ctx.combined_message or ctx.inbound_text
    if is_sold:
        vehicle = await ctx.vehicle()
        if not (_DISPOSAL.search(text) and not disposal_vetoed(text, vehicle)):
            return {"ok": False, "error": "vehicle_sold_requires_unambiguous_deterministic_disposal",
                    "suppression_category": category, "vehicle_disposed": False}

    opt_out = bool(_STOP_KEYWORD.match(ctx.inbound_text))
    deceased = category == "deceased" and bool(_DECEASED.search(text))
    effective_dnc, downgraded = permanent_dnc, False
    if permanent_dnc and not opt_out and not deceased:
        if category not in _HALT_SET:
            return {"ok": False, "error": "permanent_dnc_requires_optout_authorization", "suppression_category": category,
                    "permanent_dnc": False, "vehicle_disposed": False}
        effective_dnc, downgraded = False, True

    lead = await ctx.lead()
    if ctx.live:
        raw = await ctx.need_pool().fetchval(
            _APPLY_SUPPRESSION_SQL, lead["id"], lead["tenant_id"], reason[:500], effective_dnc, vehicle_disposed, category
        )
        r = json.loads(raw) if isinstance(raw, str) else (raw or {})
    else:
        # What apply_lead_suppression would do: DNC when authorized, halt + hold for the halt set (or a DNC), vehicle
        # disposal for vehicle_sold; lease_disclosed and polite_decline are recorded only.
        halt = effective_dnc or category in _HALT_SET
        r = {"success": True, "dry_run": True, "dnc_applied": effective_dnc, "hold_applied": halt,
             "cadences_halted": 1 if halt else 0, "vehicle_marked_disposed": is_sold}
    halted = r.get("cadences_halted") if isinstance(r.get("cadences_halted"), int) else 0
    dnc, hold, marked = r.get("dnc_applied") is True, r.get("hold_applied") is True, r.get("vehicle_marked_disposed") is True
    effective = r.get("success") is True and (r.get("suppression_effective") is True or dnc or halted > 0 or hold or marked)
    dry = {"dry_run": True} if not ctx.live else {}
    if not effective:
        return {"ok": False, "error": "suppression_noop_nothing_written", "suppression_category": category,
                "permanent_dnc": False, "vehicle_disposed": False, "nothing_written": True,
                "dnc_downgraded_to_halt": downgraded, "result": r, "message": _noop_message(category), **dry}
    if dnc:
        message = "Permanent DNC applied and cadences halted. You may say messages will stop. Do NOT claim a human was notified."
    elif halted > 0 or hold:
        message = ("Active cadences halted and a send hold is in place (no DNC row). You may say messages will stop. Do "
                   "NOT say the customer was unsubscribed, removed, opted out or taken off the list (no DNC row was "
                   "written). Do NOT claim a human was notified.")
    else:
        message = ("Vehicle marked disposed only. Do NOT say the customer is off the list or that all messages stop. Do "
                   "NOT claim a human was notified.")
    return {"ok": True, "suppression_category": category, "permanent_dnc": dnc, "vehicle_disposed": vehicle_disposed,
            "action": "suppression", "suppression_effective": True, "nothing_written": False, "dnc_applied": dnc,
            "cadences_halted": halted, "hold_applied": hold, "vehicle_marked_disposed": marked,
            "dnc_downgraded_to_halt": downgraded, "result": r, "message": message, **dry}


SUPPRESS_LEAD = Tool(
    name="Suppress_Lead",
    kind="code",
    description="Silent backend suppression, no escalation row, no team alert. What it writes depends on the RESULT, "
    "not on the request: permanent_dnc=true is honored as a DNC ONLY for deceased or a STOP-keyword turn and then "
    "writes dnc + halts cadences; for internal_employee, wrong_owner, repeat_decline and past_grievance it is "
    "downgraded to a cadence halt plus a send hold (no DNC row); vehicle_sold marks the vehicle disposed; "
    "lease_disclosed and polite_decline record only. Read the result: only ok=true with suppression_effective=true "
    "means messages will actually stop; ok=false / error=suppression_noop_nothing_written means NOTHING changed and "
    "you must not promise stoppage. CALL THIS FIRST, BEFORE composing your reply, for: lease disclosures with "
    "grievance, vehicle sold by owner (no replacement), ownership disputes, deceased contacts, internal employees, "
    "past-dealership grievances, repeat-decline frustration. Never tell the customer the team was notified, because "
    "the team is NOT notified. Do not claim a human will follow up. Do not pivot to scheduling or sales after calling "
    "this tool.",
    parameters=schema({
        "reason": {"type": "string", "description": "One-sentence summary in the customer's words explaining why suppression is being applied."},
        "suppression_category": {"type": "string", "enum": SUPPRESSION_CATEGORIES, "description": "Categorical reason. Determines downstream auditing."},
        "permanent_dnc": {"type": "boolean", "description": "Request a hard stop. Honored as a DNC row only for deceased or after a STOP keyword; for wrong_owner, internal_employee, past_grievance and repeat_decline the tool downgrades it to a cadence halt plus a send hold and reports dnc_downgraded_to_halt=true. Read suppression_effective in the result before telling the customer anything will stop. Defaults to false."},
        "vehicle_disposed": {"type": "boolean", "description": "When true, marks the primary lead_vehicle as isSold=true + ownership_status='sold'. Use for vehicle_sold category. Defaults to false."},
    }, ["reason", "suppression_category"]),
    run=suppress_lead,
    needs=(NEEDS_CUSTOMER, NEEDS_DB),
    writes=True,
)

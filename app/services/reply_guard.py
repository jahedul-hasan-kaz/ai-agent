"""
Checks on a model's reply before it goes to the customer, for models that slip out of the tool protocol.

* Tool calls written as text. Some open models (gpt-oss with little or no reasoning) write a call into the reply,
  e.g. `{"name": "Escalate_To_Human", "arguments": {...}}`, instead of using the tool-call channel, so the
  server never sees a call. `text_tool_calls` finds such JSON for a tool the agent was offered, so the gateway
  can run it as a real call (its gates apply) and keep the JSON out of the reply.
* Unevidenced hand-off claims. "I've flagged it for the team" or "a manager will call you" is only true after
  Escalate_To_Human returned ok=true in this turn (the prompts' HARD RULE 0, n8n's Output Validator).
  `unevidenced_claim` spots the claim; the caller asks the model once more, then drops the claim's sentences.
"""
import json
import re
from typing import Any, Iterable, Optional

from app.llm.base import ToolCall

ESCALATION_TOOL = "Escalate_To_Human"

_DECODER = json.JSONDecoder()
_FENCE_BEFORE = re.compile(r"```[a-zA-Z]*\s*$")
_FENCE_AFTER = re.compile(r"\s*```")


def text_tool_calls(content: str, offered: Iterable[str]) -> tuple[list[ToolCall], str]:
    """Tool calls written into `content` as JSON, for tools in `offered`, and the content without them (and without
    the ``` fence around them). Shapes: {"name": T, "arguments": {...}} (also "parameters", or the arguments as a
    JSON string), {"function": {"name": T, "arguments": ...}} and {T: {...}}. JSON naming no offered tool is left."""
    names = {n.lower(): n for n in offered}
    if not names or "{" not in content:
        return [], content
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while (start := content.find("{", i)) != -1:
        try:
            obj, end = _DECODER.raw_decode(content, start)
        except ValueError:
            i = start + 1
            continue
        call = _as_call(obj, names, len(calls))
        if call is None:
            i = start + 1
            continue
        calls.append(call)
        fence = _FENCE_BEFORE.search(content, 0, start)
        after = _FENCE_AFTER.match(content, end)
        spans.append((fence.start() if fence else start, after.end() if fence and after else end))
        i = end
    if not calls:
        return [], content
    parts, last = [], 0
    for start, end in spans:
        parts.append(content[last:start])
        last = end
    parts.append(content[last:])
    rest = re.sub(r"[ \t]+\n", "\n", "".join(parts))
    return calls, re.sub(r"\n{3,}", "\n\n", rest).strip()


def _as_call(obj: Any, names: dict[str, str], index: int) -> Optional[ToolCall]:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    if isinstance(obj.get("name"), str):
        name = _tool_name(obj["name"], names)
        args = obj.get("arguments", obj.get("parameters", {}))
    elif len(obj) == 1:
        key, args = next(iter(obj.items()))
        name = _tool_name(key, names)
    else:
        return None
    if name is None:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    return ToolCall(f"call_text_{index + 1}", name, args)


def _tool_name(raw: str, names: dict[str, str]) -> Optional[str]:
    key = raw.strip().lower()
    key = key[len("functions."):] if key.startswith("functions.") else key
    return names.get(key)


# "I've flagged it", "I'm notifying the team", "a manager will call you", "I'll have someone reach out", ...
_DONE = r"(?:flagged|notified|alerted|escalated|forwarded|looped in|let (?:the|our|a) [\w ]{1,30}? know|passed (?:this|it|that|your [\w ]{1,20}?) (?:along|on))"
_DOING = r"(?:flagging|notifying|alerting|escalating|forwarding|looping in|passing (?:this|it|that) (?:along|on))"
_WHO = r"(?:the|our|a|your) (?:\w+ ){0,2}(?:team|manager|salesperson|sales ?(?:rep|team|manager)|rep|representative|specialist|staff|person|advisor)|someone|somebody"
_CONTACT = r"(?:call|text|reach out|contact|follow up|get back|be in touch|get in touch|reach you|ring)"
_CLAIM = re.compile(
    rf"\b(?:i'?ve|i have|we'?ve|we have|i just|we just|just)\s+{_DONE}"
    rf"|\b(?:i'?m|i am|we'?re|we are)\s+(?:now\s+)?{_DOING}"
    rf"|\b(?:{_WHO})\s+(?:will|'ll|is going to|are going to|is gonna|should)\s+(?:be\s+)?{_CONTACT}"
    rf"|\b(?:i'?ll|i will|we'?ll|we will|let me)\s+(?:go ahead and\s+)?have\s+(?:{_WHO})\s+{_CONTACT}",
    re.I,
)
# A named person ("Mike will call you"): case-sensitive, so ordinary words don't count as names.
_NAMED = re.compile(rf"\b(?!I\b)[A-Z][a-z]+\s+(?:will|'ll|is going to)\s+(?:be\s+)?{_CONTACT}\b")


def _claims(text: str) -> Optional[re.Match]:
    return _CLAIM.search(text) or _NAMED.search(text)


def escalated(runs: Iterable[Any]) -> bool:
    """An Escalate_To_Human call returned ok=true in this turn (dry runs count: they report what would happen)."""
    return any(r.name == ESCALATION_TOOL and r.ok for r in runs)


def unevidenced_claim(reply: str, runs: Iterable[Any]) -> Optional[str]:
    """The phrase claiming a hand-off (someone notified, someone will contact the customer) when no Escalate_To_Human
    call succeeded this turn; None when there is no such claim or the claim is backed."""
    if escalated(runs):
        return None
    match = _claims(reply)
    return match.group(0) if match else None


def without_claims(reply: str) -> str:
    """The reply minus every sentence that makes a hand-off claim."""
    sentences = re.split(r"(?<=[.!?])\s+", reply.strip())
    return " ".join(s for s in sentences if not _claims(s)).strip()


def retry_note(runs: Iterable[Any], offered: Iterable[str]) -> str:
    """Appended to the system prompt when the model is asked again after an unevidenced claim."""
    refused = [r for r in runs if r.name == ESCALATION_TOOL and not r.ok]
    if refused:
        how = (f"{ESCALATION_TOOL} was refused this turn ({refused[-1].error or 'not ok'}). Do not call it again and do "
               "not say anyone was notified or will reach out.")
    elif ESCALATION_TOOL in set(offered):
        how = (f"If the customer needs a person and the rules allow it, call {ESCALATION_TOOL} now; otherwise do not say "
               "anyone was notified or will reach out.")
    else:
        how = "Do not say anyone was notified or will reach out."
    return (
        "\n\n=== REPLY CHECK (gateway) ===\n"
        "Your previous draft told the customer that the team, a manager or someone was notified or will contact "
        f"them, but no {ESCALATION_TOOL} call returned ok=true in this turn, so that is not true. {how} Write the "
        "reply again following your instructions; giving the dealership phone number is fine. Output only the "
        "message text."
    )


# ---- unbacked booking claims ----------------------------------------------------------------------------------
# "You're set for Friday at 4", "your appraisal is scheduled for…", "I've booked you…": true only after
# Save_Appointment_Notes saved an appointment time this turn (it refuses a time Validate_Appointment did not confirm),
# the prompts' HARD RULE 0 / 2 and n8n's Output Validator. "See you Friday" alone is not a claim (a closing may say it).
_BOOKED = r"(?:scheduled|booked|confirmed|locked\s+in|reserved|all\s+set|set|on\s+the\s+calendar|on\s+the\s+books)"
_BOOKING_CLAIM = re.compile(
    rf"\b(?:you'?re|you\s+are|we'?re|we\s+are|it'?s|it\s+is|that'?s|that\s+is|everything'?s|everything\s+is)\s+(?:all\s+)?{_BOOKED}\b"
    rf"|\byour\s+(?:\w+\s+){{0,2}}(?:appointment|appraisal|visit|test\s+drive|call|time|slot)\s+(?:is|has\s+been)\s+(?:now\s+)?{_BOOKED}\b"
    r"|\b(?:i'?ve|i\s+have|we'?ve|we\s+have)\s+(?:\w+\s+)?(?:booked|scheduled|confirmed|locked\s+in|reserved|set\s+up)\b"
    r"|\b(?:got|have|put)\s+you\s+(?:down|in|on\s+the\s+(?:calendar|schedule|books))\b"
    r"|\b(?:scheduled|booked|confirmed|locked\s+in)\s+(?:you\s+)?for\s+(?:today|tomorrow|tonight|this|next|mon|tue|wed|thu|fri|sat|sun|\d)",
    re.I,
)


def booked(runs: Iterable[Any]) -> bool:
    """Save_Appointment_Notes saved an appointment time this turn (dry runs count)."""
    return any(r.name == "Save_Appointment_Notes" and r.ok and (getattr(r, "arguments", None) or {}).get("appointment_datetime")
               for r in runs)


def unbacked_booking(reply: str, runs: Iterable[Any]) -> Optional[str]:
    """The phrase claiming an appointment is booked when no appointment was saved this turn; None otherwise."""
    runs = list(runs)
    if booked(runs):
        return None
    match = _BOOKING_CLAIM.search(reply)
    return match.group(0) if match else None


def without_booking_claims(reply: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", reply.strip())
    return " ".join(s for s in sentences if not _BOOKING_CLAIM.search(s)).strip()


BOOKING_FALLBACK = "Thanks! I want to make sure that time is open before I lock it in - what day and time work best for you?"


def booking_retry_note(offered: Iterable[str]) -> str:
    offered = set(offered)
    if {"Validate_Appointment", "Save_Appointment_Notes"} <= offered:
        how = ("Call Validate_Appointment for the exact day and time, then Save_Appointment_Notes with its "
               "confirmed_datetime, and only then confirm; if you do not, do not say it is booked.")
    else:
        how = ("You cannot book appointments: do not say anything is scheduled, booked, confirmed or set. Offer to set "
               "up the visit or return <NOOP> if this is not your topic.")
    return (
        "\n\n=== REPLY CHECK (gateway) ===\n"
        "Your previous draft told the customer an appointment is booked/scheduled/confirmed, but no appointment was "
        f"saved this turn, so that is not true. {how} Write the reply again following your instructions. Output only "
        "the message text."
    )

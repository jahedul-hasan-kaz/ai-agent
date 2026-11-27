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


def text_tool_calls(content: str, offered: Any) -> tuple[list[ToolCall], str]:
    """Tool calls written into `content` as JSON, for tools in `offered`, and the content without that JSON.

    `offered`: the offered tools by name ({name: Tool}, which also lets bare arguments be matched to a tool by its
    parameters) or just their names. The shapes gpt-oss writes (seen in the regression runs):
      {"name"|"function"|"type"|"action"|"tool": T, "arguments"|"parameters"|"args"|"input": {...}}
      {"action": T, <arguments inline>}            {"function": {"name": T, "arguments": ...}}      {T: {...}}
      a list of those, or several back to back ({...}{...} / {...},{...}), also inside a ``` fence
      {"message"|"text": "...", "calls"|"tool_calls": [...]}  and  {"action": "message", "message": "..."}
      bare arguments {"proposed_time": "..."}: the one offered tool whose parameters they fit
    Invented results ("response", "result") and keys that are not the tool's parameters (lead_id, ...) are dropped.
    A "message"/"text" written alongside becomes the remaining reply text. JSON that names no offered tool, or fits
    no tool unambiguously, is left in the text as it was."""
    tools = _offered_tools(offered)
    if not tools or not re.search(r"[{\[]", content):
        return [], content
    calls: list[ToolCall] = []
    texts: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while (m := re.compile(r"[{\[]").search(content, i)) is not None:
        start = m.start()
        try:
            value, end = _DECODER.raw_decode(content, start)
        except ValueError:
            i = start + 1
            continue
        found_calls: list[ToolCall] = []
        found_texts: list[str] = []
        _collect(value, tools, found_calls, found_texts, len(calls))
        if not found_calls and not found_texts:
            i = start + 1
            continue
        calls += found_calls
        texts += found_texts
        fence = _FENCE_BEFORE.search(content, 0, start)
        after = _FENCE_AFTER.match(content, end)
        spans.append((fence.start() if fence else start, after.end() if fence and after else end))
        i = end
    if not calls and not texts:
        return [], content
    parts, last = [], 0
    for start, end in spans:
        parts.append(content[last:start])
        last = end
    parts.append(content[last:])
    rest = "".join(parts)
    rest = re.sub(r"(?m)^[\s,\[\]]+$", "", rest)  # separators left between removed JSON values
    rest = re.sub(r"^[\s,]+|[\s,]+$", "", rest)
    if texts:
        rest = (rest + "\n" if rest else "") + "\n".join(t.strip() for t in texts if t.strip())
    rest = re.sub(r"[ \t]+\n", "\n", rest)
    return calls, re.sub(r"\n{3,}", "\n\n", rest).strip()


_NAME_KEYS = ("name", "function", "type", "action", "tool", "tool_name", "recipient")
_ARG_KEYS = ("arguments", "parameters", "args", "input")
_TEXT_KEYS = ("message", "text", "content", "reply")
_CALL_LIST_KEYS = ("calls", "tool_calls", "actions", "functions")
_INVENTED_KEYS = {"response", "result", "results", "output", "ok", "status", "id", "call_id", "lead_id", "tenant_id"}
# Keys of a tool RESULT the model echoed back ({"ok": true, ..., "message": "Do not claim ..."}): never a call, and its
# "message" is an instruction to the model, never reply text. Left untouched.
_RESULT_KEYS = {"ok", "error", "dry_run", "statusCode", "body", "is_valid", "suppression", "suppression_effective",
                "escalation_id", "would_insert", "cancelled", "schema_version", "nothing_written"}


def _offered_tools(offered: Any) -> dict[str, dict[str, Any]]:
    """{loose name key: {"name": real name, "props": set of parameters or None, "required": set}}."""
    out: dict[str, dict[str, Any]] = {}
    items = offered.items() if isinstance(offered, dict) else ((n, None) for n in offered)
    for name, tool in items:
        params = getattr(tool, "parameters", None) if tool is not None else None
        props = set((params or {}).get("properties", {})) if params else None
        out[_loose(name)] = {"name": name, "props": props, "required": set((params or {}).get("required", []))}
    return out


def _loose(name: str) -> str:
    key = name.strip().lower()
    key = key[len("functions."):] if key.startswith("functions.") else key
    return re.sub(r"[\s\-]+", "_", key)


def _collect(value: Any, tools: dict, calls: list[ToolCall], texts: list[str], offset: int) -> None:
    if isinstance(value, list):
        for item in value:
            _collect(item, tools, calls, texts, offset)
        return
    if not isinstance(value, dict):
        return
    obj = value
    if _RESULT_KEYS & set(obj):
        return  # an echoed tool result
    if isinstance(obj.get("function"), dict):  # {"type": "function", "function": {"name": ..., "arguments": ...}}
        obj = obj["function"]
    if set(obj) == {"response"} and isinstance(obj["response"], str):
        texts.append(obj["response"])  # {"response": "..."} on its own: the reply text
        return
    # A reply text the model wrapped in JSON, possibly with calls beside it.
    text = next((obj[k] for k in _TEXT_KEYS if isinstance(obj.get(k), str)), None)
    nested = next((obj[k] for k in _CALL_LIST_KEYS if isinstance(obj.get(k), list)), None)
    name_raw = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str)), None)
    if name_raw is not None and _loose(name_raw) in ("message", "reply", "text", "respond", "send_message"):
        name_raw = None  # {"action": "message", "message": "..."}: just the reply
    if nested is not None:
        _collect(nested, tools, calls, texts, offset)
    if name_raw is not None:
        tool = tools.get(_loose(name_raw))
        if tool is None:
            return  # names a tool this agent was not offered (or one that does not exist): leave the JSON alone
        args = next((obj[k] for k in _ARG_KEYS if k in obj), None)
        if args is None:  # arguments written inline: {"action": T, "note": ...}
            args = {k: v for k, v in obj.items() if k not in _NAME_KEYS and k not in _TEXT_KEYS}
        _add(tool, args, calls, offset)
        return
    if len(obj) == 1:  # {T: {...}}
        key, args = next(iter(obj.items()))
        tool = tools.get(_loose(key))
        if tool is not None:
            _add(tool, args, calls, offset)
            return
    # Nested argument objects beside a text ({"message": ..., "appointment_notes": {...}}) or bare arguments.
    if text is not None:
        texts.append(text)
        for key, sub in obj.items():
            if key not in _TEXT_KEYS and isinstance(sub, dict):
                tool = _fit(sub, tools)
                if tool is not None:
                    _add(tool, sub, calls, offset)
        return
    tool = _fit(obj, tools)
    if tool is not None:
        _add(tool, obj, calls, offset)


# Never guessed from bare arguments: a wrong guess would cancel a booking, stop a customer's messages or page the
# team. These run only when the model names them.
_NO_GUESS = {"Cancel_Appointment", "Suppress_Lead", "Escalate_To_Human"}


def _fit(args: dict, tools: dict) -> Optional[dict]:
    """The single offered tool whose parameters these bare arguments are (all keys are its parameters, its required
    ones present); None when none or several fit, the tools' parameters are unknown, or the only fit is a tool with
    side effects that must not be guessed (_NO_GUESS)."""
    keys = {k for k in args if k not in _INVENTED_KEYS}
    if not keys:
        return None
    fits = [t for t in tools.values() if t["props"] is not None and keys <= t["props"] and t["required"] <= keys
            and t["name"] not in _NO_GUESS]
    return fits[0] if len(fits) == 1 else None


def _add(tool: dict, args: Any, calls: list[ToolCall], offset: int) -> None:
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            return
    if not isinstance(args, dict):
        return
    if tool["props"] is not None:
        args = {k: v for k, v in args.items() if k in tool["props"]}  # drop invented results and stray keys
    else:
        args = {k: v for k, v in args.items() if k not in _INVENTED_KEYS}
    calls.append(ToolCall(f"call_text_{offset + len(calls) + 1}", tool["name"], args))


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

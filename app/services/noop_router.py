"""
What to do when an agent answers `<NOOP>`: a port of the n8n SMS handler's "Empty Reply Detector" and "NOOP Pivot
Re-Router" nodes.

A lane agent's prompt licenses `<NOOP>` in two situations, and they need opposite handling:

* Termination: the customer sent a closing ("thanks", "ok", "see you then", an emoji reaction). Nothing is sent.
* Pivot: the customer wrote something real that belongs to another agent ("what's my car worth?" to Inventory). The
  prompt promises "the workflow detects the pivot and re-routes this same turn", so it must never be silence: the
  turn is run once more on the agent the message's wording points to, and if that agent also answers `<NOOP>`, a
  safe fallback is sent and a person is paged.

`classify` / `reroute_target` / `fallback_text` hold the n8n rules; app/services/chat_service.py runs the flow.
"""
import re
from dataclasses import dataclass
from typing import Optional

NOOP = "<NOOP>"
LANES = ("inventory", "scheduling", "value_data_collection", "service_trade_reply")
GENERAL = "general"

# --- the reply ---------------------------------------------------------------------------------------------------

_NOOP_SENTINEL = re.compile(r"^(?:noop|<{1,3}\s*noop\s*/?>{1,3}|\[\s*noop\s*\]|\(\s*noop\s*\)|\{\s*noop\s*\})$", re.I)


def is_noop(reply: str) -> bool:
    """The reply is the no-send token in any of the spellings models produce (noop, <<NOOP>>, [noop], "<NOOP>." ...)."""
    comparable = re.sub(r"^[-.>…\s\"'*`]+", "", reply.strip())
    comparable = re.sub(r"[\s\"'*`.!?,;:\-—…]+$", "", comparable)
    return bool(_NOOP_SENTINEL.match(comparable))


# --- the customer's message: closing or substantive ----------------------------------------------------------------

_CLOSING = re.compile(
    r"^(?:(?:ok(?:ay)?|k|kk|yes|yep|yup|ya|yeah|no|nope|nah|sure|thanks?|thank you|thx|ty|sounds good|got it|good|great"
    r"|perfect|awesome|cool|no problem|np|you too|u too|same to you|will do|bye|goodbye|later|take care"
    r"|have a good (?:day|night|one)|good (?:morning|afternoon|evening|night)|lol|haha)\s*)+$"
)
_SEE_YOU_PREFIX = re.compile(
    r"^(?:(?:ok(?:ay)?|yes|yep|yup|sure|will do|take care|bye|no problem|thanks?|thank you|sounds good|got it|great"
    r"|perfect|awesome|cool)\s+)+(?=see (?:you|ya)(?: |$))"
)
_SEE_YOU = re.compile(
    r"^see (?:you|ya)(?: (?:(?:then|soon|there|later|next week|in the (?:morning|afternoon|evening))"
    r"|(?:(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|(?:this )?(?:morning|afternoon|evening))"
    r"(?: at \d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)|(?:at )?\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?$"
)
_APPRECIATE = re.compile(r"^appreciate (?:it|that|you)$")
_GRATITUDE = re.compile(r"^(?:thanks|thank you)(?: (?:so much|very much|a lot|again)| for (?:your|the) (?:help|info|update))$")
_POSITIVE_GRATITUDE = re.compile(r"^(?:perfect|great|awesome|sounds good|got it) (?:thanks?|thank you)$")
_BARE_AFFIRMATIVE = re.compile(r"^(?:(?:ok(?:ay)?|k|kk|yes|yep|yup|ya|yeah|sure|sounds good|got it|will do|great|perfect|awesome|cool)\s*)+$")

_ZW = r"\s​-‏﻿"
_TAPBACK_WORD = re.compile(
    rf"^[{_ZW}]*(?:liked|loved|laughed at|emphasi[sz]ed|questioned|disliked|removed\s+\S+\s+from|reaction|реакция"
    r"|отреагировал\S*|le\s+gust\S*|le\s+encant\S*|a\s+aim\S*|a\s+ador\S*|no\s+le\s+gust\S*|se\s+ri[oó]\s+de"
    r"|le\s+dio\s+risa|enfatizó|destacó|exclamó\s+por|preguntó|reaccionó\s+con\s+\S+\s+a|eliminó\s+\S+\s+de"
    r"|se\s+quit[oó]\s+\S+\s+de|curtiu|gostou\s+de|用\S{1,8}回应了)[^\"“«]{0,24}[\"“«]",
    re.I,
)
_TAPBACK_QUOTE = re.compile(rf"^[{_ZW}]*[^\w\s]{{1,8}}[{_ZW}]*(?:to|a|на)?[{_ZW}]*[\"“«][\s\S]*[\"”»][{_ZW}]*$")

_SIGNAL = re.compile(r"^(?:" + "|".join([
    r"thanks|thank you|thank u|thank ya|thankyou|thanx|thx|tks|txs|thks|thnx|ty|much appreciated|many thanks",
    r"(?:i |we )?(?:really |truly |much )?appreciate(?: (?:it|that|you|the help|your help|the info(?:rmation)?|you all))?",
    r"(?:you|u) (?:too|two|to|as well)|same to you|(?:and )?you (?:do )?the same",
    r"you do the same|(?:you|u|your|youre|you re|ur) (?:are )?welcome",
    r"bye|goodbye|later|talk soon|talk to you (?:soon|later)|ttyl|cheers",
    r"(?:you )?(?:take care|be safe|stay safe|god bless|bless you)(?: (?:too|as well|also))?",
    r"(?:have|enjoy) (?:a|an|the|your) (?:\w+ ){0,3}(?:rest of (?:your|the) )?(?:\w+ ){0,2}(?:day|night|one|evening|morning|afternoon|weekend|week)",
]) + r")\b\s*")
_FILLER = re.compile(r"^(?:" + "|".join([
    r"ok(?:ay)?|okey|kk|alright(?:y)?|yes|yep|yup|ya|yeah|sure(?: thing)?|no problem|np",
    r"will do|wilco|can do",
    r"sounds? (?:good|great)|got it|good|great|perfect|awesome|cool|excellent|lol|haha",
    r"copy(?: that)?|roger(?: that)?|10 4|gotcha|understood|noted|bet|word",
    r"though|anyway|anyhow|again|so (?:much|very much)|very much|a lot|a bunch|kindly|much",
    r"for (?:the |your |all |all the )?(?:\w+ ){0,2}(?:reply|response|help|info(?:rmation)?|update|time|call|text|message"
    r"|asking|reaching out|checking in|letting me know|everything)",
    r"and|also|too|as well|mr|mrs|ms",
    r"(?:my )?(?:friend|man|dude|bro(?:ther)?|buddy|pal|boss|chief|fam|sir|maam|ma am)",
]) + r")\b\s*")
_MEANINGFUL = {
    "why", "when", "how", "who", "where", "what", "call", "text", "send", "sold", "keep", "stop", "help", "wait", "price",
    "value", "trade", "offer", "miles", "cash", "yes", "no", "but", "still", "not", "need", "want", "cant", "wont",
    "fix", "broken", "issue", "problem", "soon", "today", "tomorrow", "now", "asap", "maybe", "if", "will", "can", "do",
    "done", "go", "come", "buy", "sell", "please", "share", "interested", "time", "check", "look", "ready", "book",
    "appointment", "schedule", "more", "less", "number", "quote", "info", "details", "yet",
}
_COMMITMENT = re.compile(r"\b(?:yes|yep|yeah|yup|sure|ok|okay|will do|i will|we will|absolutely|of course|sounds good)\b")


def _asked_question(previous_outbound: str) -> bool:
    text = re.sub(r"\s*Reply\s+STOP\s+to\s+opt\s+out\.?\s*$", "", previous_outbound or "", flags=re.I).strip()
    return text.endswith("?")


def _closing_by_shape(norm: str, asked: bool) -> bool:
    """n8n closing-shape-v1: consume closing signals and filler from the front; a closing has at least one signal
    and leaves nothing (or one name-like word) behind."""
    rest = norm
    if not rest or (asked and _COMMITMENT.search(rest)):
        return False
    saw_signal, prev = False, None
    while rest and rest != prev:
        prev = rest
        if _SIGNAL.match(rest):
            saw_signal = True
            rest = _SIGNAL.sub("", rest, count=1)
            continue
        rest = _FILLER.sub("", rest, count=1)
    if not saw_signal:
        return False
    return rest == "" or (re.fullmatch(r"[a-z]{2,12}", rest) is not None and rest not in _MEANINGFUL)


def is_closing(message: str, previous_outbound: str = "") -> bool:
    """The customer's message only closes the conversation (thanks, ok, bye, see you then ...). A bare "yes"/"ok" that
    answers a question we just asked is an answer, not a closing."""
    norm = re.sub(r"[^a-z0-9$?]+", " ", message.lower()).strip()
    time_text = re.sub(r"(?:[:;=8][-']?[()dDpP/\\|*]+\s*)+$", "", message).strip()
    see_you = _SEE_YOU_PREFIX.sub("", re.sub(r"[^a-z0-9$?:]+", " ", time_text.lower()).strip(), count=1)
    asked = _asked_question(previous_outbound)
    if asked and _BARE_AFFIRMATIVE.match(norm):
        return False
    return bool(
        _CLOSING.match(norm) or _SEE_YOU.match(see_you) or _APPRECIATE.match(norm) or _GRATITUDE.match(norm)
        or _POSITIVE_GRATITUDE.match(norm) or _closing_by_shape(norm, asked)
    )


def is_tapback(message: str) -> bool:
    """An emoji reaction to one of our messages (Liked "…", ❤️ to "…"), which arrives as an ordinary SMS."""
    return bool(_TAPBACK_WORD.match(message) or _TAPBACK_QUOTE.match(message))


def is_substantive(message: str, previous_outbound: str = "") -> bool:
    """A real message that deserves an answer: not empty, not a reaction, not a closing."""
    norm = re.sub(r"[^a-z0-9$?]+", " ", message.lower()).strip()
    return not is_tapback(message) and len(norm) >= 2 and not is_closing(message, previous_outbound)


# --- where a pivot goes --------------------------------------------------------------------------------------------

_SCHEDULING_HINT = re.compile(
    r"\b(?:appointment|appt|schedule|reschedul\w*|book(?:ing)?|come (?:in|by)|stop (?:in|by)|swing by|are you open"
    r"|what time do you (?:open|close)|(?:mon|tues?|wednes|thurs?|fri|satur|sun)day|tomorrow|today|tonight"
    r"|this (?:week|weekend)|next week|noon|morning|afternoon|evening|\d{1,2}(?::\d{2})?\s*(?:am|pm)\b)",
    re.I,
)
_VALUE_HINT = re.compile(
    r"\b(?:worth|value|trade.?in|trade\b.{0,40}\bin\b|offer|appraisal|appraise|how much|payoff|buy ?out"
    r"|sell(?:ing)? (?:it|my|the)|what (?:would|will|do) you (?:give|pay|offer))\b",
    re.I,
)
_INVENTORY_HINT = re.compile(r"\b(?:in stock|do you have (?:any|a)\b|got any|inventory|on the lot|available (?:in|to)|looking for a)\b", re.I)


def reroute_target(message: str, origin: str) -> str:
    """n8n NOOP Pivot Re-Router: the rescue agent from the message's wording (scheduling, then value, then inventory;
    otherwise general), never the agent that just declined it, except general, which gets a second opinion."""
    if _SCHEDULING_HINT.search(message):
        route = "scheduling"
    elif _VALUE_HINT.search(message):
        route = "value_data_collection"
    elif _INVENTORY_HINT.search(message):
        route = "inventory"
    else:
        route = GENERAL
    return GENERAL if route == origin else route


# --- when the second agent declines too ----------------------------------------------------------------------------


def fallback_text(lane: str, first_name: str = "", complaint: bool = False) -> str:
    """n8n's SAFE_FALLBACK for a turn two agents declined: the complaint copy, the general copy (general and service
    lanes) or the value copy (the other lanes)."""
    name = (first_name or "").strip()
    if complaint:
        lead_in = f"Hey {name} - I'm sorry about that." if name else "I'm sorry about that."
        return f"{lead_in} I've flagged it for our team and someone will follow up with you shortly."
    if lane in (GENERAL, "service_trade_reply"):
        body = "let me take a quick look into that, and someone from our team will follow up shortly."
    else:
        body = "give me a few minutes to pull this up properly, and someone from our team will follow up shortly with the right number."
    return f"Hey {name} — {body}" if name else body[0].upper() + body[1:]


@dataclass
class Routing:
    """What happened to a `<NOOP>` turn, reported in the API response."""

    outcome: str  # silent_closing | silent_suppressed | silent_escalated | rerouted | fallback | silent_confirmed
    from_agent: str
    to_agent: Optional[str] = None

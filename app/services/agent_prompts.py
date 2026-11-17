"""
Agent system prompts: one per file in app/prompts/agents/<agent>_system_instr.md.

The files are read, never written. Each is prepared once per process:

* n8n template expressions (`{{ $('Get tenant').first().json.slug ?? 'the dealership' }}`) only make sense
  inside the n8n workflow, so each is replaced by the fallback the expression itself names ('the dealership').
  The current date and time point to a small runtime block instead (see `runtime_context`), so the prompt
  text stays byte-identical across requests and providers can cache it as a prefix.
* Whitespace is tidied (trailing spaces, runs of blank lines). No wording changes.

The request's `agent` picks the file; without one the `general` agent is used.
"""
import math
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.llm.base import LLMError

AGENTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "agents"
SUFFIX = "_system_instr.md"
DEFAULT_AGENT = "general"
DEFAULT_TIMEZONE = "America/New_York"

# Prompts are dense with punctuation and markup, which tokenizes worse than prose: 3.5 chars/token errs on
# the side of overestimating, so a budget computed from it doesn't overflow the model's window.
CHARS_PER_TOKEN = 3.5

_EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.S)
_LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'")
_DATE_FIELDS = {"date": "current date", "time_12h": "current time", "day_of_week": "day of week"}


@dataclass(frozen=True)
class AgentPrompt:
    id: str
    label: str
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


# The id callers send when it differs from the file's name (schedule_system_instr.md -> "scheduling").
FILE_TO_AGENT = {"schedule": "scheduling"}
# Other spellings accepted in `agent`.
ALIASES = {"schedule": "scheduling", "value": "value_data_collection", "service": "service_trade_reply"}


def normalize(agent_id: Optional[str]) -> str:
    """'Value Data-Collection ' -> 'value_data_collection'; empty -> the default agent."""
    key = re.sub(r"[\s\-]+", "_", (agent_id or "").strip().lower()) or DEFAULT_AGENT
    return ALIASES.get(key, key)


@lru_cache
def agents() -> dict[str, AgentPrompt]:
    """Every agent prompt on disk, `general` first, then alphabetical."""
    found = {}
    for path in sorted(AGENTS_DIR.glob(f"*{SUFFIX}")):
        agent_id = path.name[: -len(SUFFIX)]
        agent_id = FILE_TO_AGENT.get(agent_id, agent_id)
        found[agent_id] = AgentPrompt(agent_id, agent_id.replace("_", " ").title(), prepare(path.read_text(encoding="utf-8")))
    return dict(sorted(found.items(), key=lambda kv: (kv[0] != DEFAULT_AGENT, kv[0])))


def get_agent(agent_id: Optional[str]) -> AgentPrompt:
    available = agents()
    agent = available.get(normalize(agent_id))
    if agent is None:
        choices = ", ".join(available) or f"none — no *{SUFFIX} file in app/prompts/agents"
        raise LLMError(
            f"Unknown agent '{agent_id}'. Available agents: {choices}.",
            400, retryable=False, code="unknown_agent",
        )
    return agent


def prepare(raw: str) -> str:
    text = _EXPRESSION.sub(lambda m: _render(m.group(1)), raw)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _render(expression: str) -> str:
    """The value an n8n expression falls back to when its node has no data."""
    if "Get Current Date/Time" in expression:
        field = next((label for key, label in _DATE_FIELDS.items() if f"json.{key}" in expression), "current date and time")
        return f"(see {field} under RUNTIME CONTEXT)"
    if "=>" in expression:  # a whole JS function (the opening-hours table); there is no data to run it on
        return "(not provided)"
    literals = _LITERAL.findall(expression)
    if not literals:
        return "(not provided)"
    value = literals[-1]
    # Workflow sentinels such as '__INTERNAL_NULL_VALUE_TRIPWIRE__' must never reach a customer.
    return "unavailable" if value.startswith("__") else value


def runtime_context(now: Optional[datetime] = None, timezone: str = DEFAULT_TIMEZONE) -> str:
    """The per-request facts the prompt's date/time placeholders point to. Kept out of the agent text
    so that text is an unchanging, cacheable prefix."""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz, timezone = ZoneInfo("UTC"), "UTC"
    local = (now or datetime.now(tz)).astimezone(tz)
    clock = f"{local:%I:%M %p}".lstrip("0")
    return (
        "=== RUNTIME CONTEXT ===\n"
        f"Current date: {local:%Y-%m-%d}\n"
        f"Current time: {clock} ({timezone})\n"
        f"Day of week: {local:%A}"
    )

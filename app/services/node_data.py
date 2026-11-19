"""
Values the n8n SMS handler's upstream nodes hand to its agents and tools.

In the workflow an agent's prompt and its tools read other nodes directly (`$('Get tenant').first().json.slug`,
the Tool Context node's `classifier: $('Intent Classifier').first().json`). Here:

* database-backed nodes (DATABASE_NODES: the tenant, lead, vehicle, SMS history, ...) are read by the gateway from
  tenant_id and the thread key, and the clock (SYSTEM_NODES) is the gateway's own: these are never supplied;
* the other computed nodes (the Intent Classifier, the combined message, ...) can be supplied on a request as
  `node_data = {"<node name>": <that node's json>}`; anything left out keeps the gateway's own behaviour (a value
  derived from the messages, or the prompt's fallback).
"""
import re
from typing import Any, Iterable, Optional

NodeData = dict[str, Any]

# Nodes that read or write the backend's database. The gateway reads these itself (tenant_id, thread key), so a
# request never supplies them: Supabase "get" nodes and REST/RPC calls in the n8n SMS handler.
DATABASE_NODES = frozenset({
    "Get tenant",  # tenants
    "Get tenant emails & phone",  # tenants
    "Lookup Lead",  # leads
    "Lookup Lead Vehicle",  # rpc resolve_thread_vehicle
    "Get Recent SMS History",  # sms_log
    "Insert Log",  # sms_log (write)
    "Get Equity Context",  # rpc get_lead_equity_context
    "Load Conversation State",  # sms_conversation_state
})
# Computed nodes the gateway works out itself.
SYSTEM_NODES = frozenset({
    "Get Current Date/Time",  # the server's clock, see agent_prompts.runtime_context
})
# Everything a request may not supply in node_data.
INTERNAL_NODES = DATABASE_NODES | SYSTEM_NODES

# $('Node').first().json.a?.b  /  $('Node').item.json.a
_NODE_REF = re.compile(r"\$\('([^']+)'\)\.(?:first\(\)|last\(\)|item)\.json((?:\??\.[A-Za-z_][A-Za-z0-9_]*)+)")


def refs(text: str) -> list[tuple[str, str]]:
    """(node, field path) for every node read in an n8n expression, in order: `a?.b` -> 'a.b'."""
    return [(node, path.replace("?", "").lstrip(".")) for node, path in _NODE_REF.findall(text)]


def node(node_data: Optional[NodeData], name: str) -> Any:
    """The supplied output of one node, or None."""
    return (node_data or {}).get(name)


def value(node_data: Optional[NodeData], name: str, path: str) -> Any:
    """One field of a supplied node (`hours_of_operation.timezone`), or None when the node or field is absent."""
    current: Any = node(node_data, name)
    for key in path.split(".") if path else ():
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def present(v: Any) -> bool:
    return v is not None and not (isinstance(v, str) and not v.strip())


def merge_fields(pairs: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """{node: [fields]} from (node, field) pairs, first-seen order, no duplicates."""
    out: dict[str, list[str]] = {}
    for name, path in pairs:
        fields = out.setdefault(name, [])
        if path and path not in fields:
            fields.append(path)
    return out

"""
Values the n8n SMS handler's upstream nodes hand to its agents and tools, supplied on a request as `node_data`.

In the workflow an agent's prompt and its tools read other nodes directly (`$('Get tenant').first().json.slug`,
the Tool Context node's `classifier: $('Intent Classifier').first().json`). Here a caller can pass those nodes'
outputs as `node_data = {"<node name>": <that node's json>}`. Whatever is supplied wins; anything left out keeps
the gateway's own behaviour (a database lookup, a value derived from the messages, or the prompt's fallback).
"""
import re
from typing import Any, Iterable, Optional

NodeData = dict[str, Any]

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

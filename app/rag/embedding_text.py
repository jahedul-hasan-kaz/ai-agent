"""
What gets embedded for a `docs` row.

Every text field goes in (kind, tags, content, and the text/number/bool values
inside metadata) so a customer's wording can match any of them. Datetimes and
GUIDs are left out: `id`, `tenant_id`, `cadence_id`, `updated_at`, and any
metadata value that is one. They carry no meaning for similarity and only pull
unrelated rows together (two docs "match" because both mention a date or UUID).

`image_path` is also left out: it is a storage path, usually built from a GUID.
"""
import re
from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

# text-embedding-3 accepts 8191 tokens; ~4 chars/token, with headroom for non-English text.
MAX_EMBED_CHARS = 24_000

_GUID = re.compile(r"^\{?[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\}?$", re.IGNORECASE)
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?$", re.IGNORECASE
)
_MAX_DEPTH = 5


def is_guid(value: Any) -> bool:
    return isinstance(value, UUID) or (isinstance(value, str) and bool(_GUID.match(value.strip())))


def is_datetime(value: Any) -> bool:
    return isinstance(value, (datetime, date)) or (isinstance(value, str) and bool(_DATETIME.match(value.strip())))


def build_embedding_text(
    kind: str,
    content: str,
    tags: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
    max_chars: int = MAX_EMBED_CHARS,
) -> str:
    parts = [f"kind: {kind.strip()}"]
    tag_text = ", ".join(t.strip() for t in tags or [] if t and t.strip() and not _skip(t))
    if tag_text:
        parts.append(f"tags: {tag_text}")
    parts.append(content.strip())
    meta_lines = list(_flatten(metadata, "", 0))
    if meta_lines:
        parts.append("\n".join(meta_lines))
    return "\n\n".join(parts)[:max_chars]


def _skip(value: Any) -> bool:
    return is_guid(value) or is_datetime(value)


def _flatten(node: Any, path: str, depth: int):
    """`key: value` lines for every text, number and bool leaf; nested keys are dotted."""
    if node is None or depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flatten(value, f"{path}.{key}" if path else str(key), depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _flatten(item, path, depth + 1)
    elif path and not _skip(node):
        text = str(node).strip()
        if text:
            yield f"{path}: {text}"

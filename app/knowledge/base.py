"""
The dealership knowledge base. It is static: the instructions and every entry are written out in
app/prompts/knowledge_base_system.md, and that whole file is part of every agent's system prompt (see context.py).
Nothing is read from the `docs` table.

Knowledge_Base_Search looks through the same entries by keyword: they are parsed here from the file's
`<knowledge>` block, one entry per `[kind] tags: … | metadata: …` header line followed by its text.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "knowledge_base_system.md"

_BLOCK = re.compile(r"<knowledge>\n(.*?)\n</knowledge>", re.S)
_HEADER = re.compile(r"^\[(?P<kind>[\w-]+)\](?: tags: (?P<tags>.*?))?(?: \| metadata: (?P<metadata>.*))?$")


@dataclass
class KnowledgeDoc:
    kind: str
    tags: list[str]
    content: str
    metadata: dict[str, str] = field(default_factory=dict)


@lru_cache
def knowledge_base_system() -> str:
    """The whole block every agent gets: instructions plus the <knowledge> entries."""
    return PROMPT.read_text(encoding="utf-8").strip()


@lru_cache
def entries() -> tuple[KnowledgeDoc, ...]:
    return tuple(parse_entries(knowledge_base_system()))


def parse_entries(text: str) -> list[KnowledgeDoc]:
    """The entries of the `<knowledge>` block, in file order."""
    match = _BLOCK.search(text)
    items: list[KnowledgeDoc] = []
    body: list[str] = []
    for line in (match.group(1).splitlines() if match else []):
        header = _HEADER.match(line)
        if header:
            _close(items, body)
            body = []
            items.append(KnowledgeDoc(
                kind=header["kind"],
                tags=[t.strip() for t in (header["tags"] or "").split(",") if t.strip()],
                content="",
                metadata=_metadata(header["metadata"] or ""),
            ))
        elif items:
            body.append(line)
    _close(items, body)
    return items


def _close(items: list[KnowledgeDoc], body: list[str]) -> None:
    if items and not items[-1].content:
        items[-1].content = "\n".join(body).strip()


def _metadata(text: str) -> dict[str, str]:
    out = {}
    for pair in text.split(","):
        key, sep, value = pair.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


# --- Knowledge_Base_Search ---------------------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "a an and are as at be but by can could do does did for from had has have how i if in is it its me my of on "
    "or our so that the their them then there they this to was we what when where which who why will with would "
    "you your yours about any just get got"
    .split()
)


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and (len(w) > 2 or w.isdigit())}


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def search(items, question: str, limit: int) -> list[tuple[KnowledgeDoc, int]]:
    """The entries sharing the most words with the question (a tag match counts double), best first."""
    wanted = _terms(question)
    if not wanted:
        return []
    scored = []
    for i, doc in enumerate(items):
        tag_terms = _terms(" ".join(doc.tags).replace("-", " ").replace("_", " "))
        score = len(wanted & _terms(doc.content)) + 2 * len(wanted & tag_terms)
        if score:
            scored.append((score, -i, doc))
    scored.sort(reverse=True, key=lambda s: (s[0], s[1]))
    return [(doc, score) for score, _, doc in scored[:limit]]

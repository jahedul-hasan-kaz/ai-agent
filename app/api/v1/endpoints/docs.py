from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.llm.base import LLMError
from app.models.docs import DocCreate, DocDeleted, DocOut, DocUpdate
from app.rag.store import DocStore

router = APIRouter(prefix="/docs")

_ERRORS = {
    400: {"description": "Invalid doc, or a tenant_id / cadence_id that does not exist"},
    401: {"description": "Missing or invalid API key"},
    404: {"description": "No such doc for this tenant"},
    409: {"description": "The doc changed while it was being re-embedded; retry"},
    502: {"description": "The embedding model or database failed; nothing was saved"},
    503: {"description": "Knowledge base not configured or unavailable"},
}


def get_doc_store(request: Request) -> DocStore:
    store = request.app.state.doc_store
    if store is None:
        raise LLMError(
            "The knowledge base is not configured: add a `rag` section to ai-agent-config and set PG_DB_URL.",
            503, retryable=False, code="docs_unavailable",
        )
    return store


@router.post("", status_code=201, response_model=DocOut, responses=_ERRORS)
async def create_doc(body: DocCreate, store: DocStore = Depends(get_doc_store)):
    """Store a doc and its embedding. The embedding is built from kind, tags, content and the text values in
    metadata; datetimes and GUIDs are not embedded. Nothing is saved if the embedding fails."""
    return await store.create(body)


@router.put("/{doc_id}", response_model=DocOut, responses=_ERRORS)
async def update_doc(doc_id: UUID, body: DocUpdate, store: DocStore = Depends(get_doc_store)):
    """Change only the fields sent. The embedding is regenerated when kind, tags, content or metadata change."""
    return await store.update(doc_id, body)


@router.delete("/{doc_id}", response_model=DocDeleted, responses=_ERRORS)
async def delete_doc(doc_id: UUID, tenant_id: UUID = Query(...), store: DocStore = Depends(get_doc_store)):
    return await store.delete(doc_id, tenant_id) or DocDeleted(id=doc_id)

"""
The web UI's chat tester: pick a dealership and a customer's SMS or email
thread, read its history, and chat through the same completion as
/v1/chat/completions. No API key (the browser can't hold it), so the whole
router is off unless playground.enabled is set in ai-agent-config.
"""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_chat_service
from app.api.v1.endpoints.chat import create_chat_completion, list_agents, list_models
from app.customer import playground
from app.llm.base import LLMError
from app.models.chat import ChatCompletionRequest
from app.services.chat_service import ChatService


def playground_enabled(request: Request) -> None:
    if not get_chat_service(request).manager.config.playground.enabled:
        raise LLMError("The chat tester is turned off (playground.enabled).", 404, retryable=False, code="not_found")


router = APIRouter(prefix="/playground", dependencies=[Depends(playground_enabled)])


async def _pool(service: ChatService) -> asyncpg.Pool:
    store = service.customers
    if store is None or not (store.ready or await store._reconnect()):
        raise LLMError("Customer records database unavailable.", 503, retryable=True, code="db_unavailable")
    return store._pool


@router.get("/tenants")
async def list_tenants(service: ChatService = Depends(get_chat_service)):
    return {"data": await playground.tenants(await _pool(service))}


@router.get("/threads")
async def list_threads(
    tenant_id: Optional[UUID] = None,
    channel: Literal["sms", "email"] = "sms",
    search: Optional[str] = Query(None, max_length=100),
    customer: Optional[str] = Query(None, max_length=100),
    text: Optional[str] = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    service: ChatService = Depends(get_chat_service),
):
    """
    Customers with a thread, most recent first: at one dealership, or at all of them without tenant_id.
    search matches the customer or dealership name; customer the customer's name, number (any formatting)
    or email; text only keeps threads with a message containing it, previewing the newest match.
    """
    return {
        "data": await playground.threads(
            await _pool(service), tenant_id, channel, search, limit, customer=customer, text=text
        )
    }


@router.get("/history")
async def thread_history(
    tenant_id: UUID,
    key: str = Query(..., min_length=1, max_length=255),
    channel: Literal["sms", "email"] = "sms",
    limit: int = Query(50, ge=1, le=200),
    before: Optional[datetime] = None,
    service: ChatService = Depends(get_chat_service),
):
    """One page of the thread, oldest first. For older messages pass before = the first message's created_at."""
    return await playground.history(await _pool(service), tenant_id, channel, key, limit, before)


@router.get("/models")
async def playground_models(service: ChatService = Depends(get_chat_service)):
    return await list_models(service)


@router.get("/agents")
async def playground_agents():
    """The chat tester's agent picker: same list as /v1/agents."""
    return await list_agents()


@router.post("/chat/completions")
async def playground_chat(body: ChatCompletionRequest, service: ChatService = Depends(get_chat_service)):
    """Same request and response as /v1/chat/completions."""
    return await create_chat_completion(body, service)

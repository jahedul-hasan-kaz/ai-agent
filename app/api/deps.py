import secrets
from typing import Optional

from fastapi import Header, Request

from app.core.config import EnvEnum, config
from app.llm.base import LLMError
from app.services.chat_service import ChatService


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


def verify_api_key(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> None:
    """`Authorization: Bearer <service_api_key>` or `X-API-Key`, checked against ai-agent-config."""
    expected = get_chat_service(request).manager.config.service_api_key
    if not expected:
        if config.ENV == EnvEnum.dev:
            return  # dev without a key: open, warned at startup
        raise LLMError("service_api_key is not set in ai-agent-config.", 503, retryable=False, code="auth_not_configured")

    supplied = x_api_key
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise LLMError("Invalid or missing API key.", 401, retryable=False, code="invalid_api_key")

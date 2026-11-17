from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.api.v1.api import router as api_v1_router
from app.api.v1.endpoints import playground
from app.customer.store import CustomerStore
from app.core.config import EnvEnum, config
from app.llm.base import LLMError
from app.llm.model_manager import ModelManager
from app.rag.retriever import Retriever
from app.rag.store import DocStore
from app.services.agent_prompts import DEFAULT_AGENT, agents
from app.services.chat_service import ChatService, error_body


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent_config = config.load_agent_config()
    if not agent_config.service_api_key:
        logger.warning("service_api_key is empty — API auth is DISABLED (dev only; prod refuses requests).")

    manager = ModelManager(agent_config)
    logger.info("Models loaded: {}", ", ".join(f"{m.id} ({m.type.value}/{m.provider})" for m in manager.models))
    loaded = agents()
    if DEFAULT_AGENT not in loaded:
        logger.error("No {} agent prompt in app/prompts/agents — requests without `agent` will fail.", DEFAULT_AGENT)
    logger.info("Agents loaded: {}", ", ".join(f"{a.id} (~{a.tokens} tokens)" for a in loaded.values()) or "none")
    largest = max((a.tokens for a in loaded.values()), default=0)
    for m in manager.models:
        if m.context_window is None:
            logger.warning("model {} has no context_window: prompts (up to ~{} tokens) are sent unfitted.", m.id, largest)
        elif m.context_window < largest + m.max_tokens:
            logger.warning("model {} context_window={} is too small for the largest agent prompt (~{} tokens).",
                           m.id, m.context_window, largest)
    retriever = await _start_retriever(agent_config)
    customers = await _start_customers(agent_config)
    app.state.chat_service = ChatService(manager, retriever, customers)
    app.state.doc_store = DocStore(retriever) if retriever else None
    app.state.cors_origins = agent_config.playground.cors_origins if agent_config.playground.enabled else []

    yield

    await manager.aclose()
    if retriever:
        await retriever.aclose()
    if customers:
        await customers.aclose()


def _pg_dsn() -> str:
    try:
        return config.load_pg_db_url()
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not read the Postgres URL: {}", exc)
        return ""


async def _start_retriever(agent_config) -> Retriever | None:
    rag = agent_config.rag
    if not rag or not rag.enabled:
        logger.warning("rag is not configured — replies are not grounded in the docs knowledge base.")
        return None
    dsn = _pg_dsn()
    if not dsn:
        logger.error("rag is configured but PG_DB_URL is empty — replies are not grounded in the docs knowledge base.")
        return None
    retriever = Retriever(rag, dsn)
    await retriever.start()
    return retriever


async def _start_customers(agent_config) -> CustomerStore | None:
    """Customer records and history for requests with sms_thread_key / email_thread_key."""
    if not agent_config.customer.enabled:
        logger.warning("customer is disabled — sms_thread_key / email_thread_key load no records or history.")
        return None
    dsn = _pg_dsn()
    if not dsn:
        logger.error("PG_DB_URL is empty — sms_thread_key / email_thread_key load no records or history.")
        return None
    store = CustomerStore(agent_config.customer, dsn)
    await store.start()
    return store


is_prod = config.ENV == EnvEnum.prod
app = FastAPI(
    title=config.PROJECT_NAME,
    description="Model gateway for dealer automotive conversations.",
    lifespan=lifespan,
    docs_url=None if is_prod else "/docs",
    redoc_url=None if is_prod else "/redoc",
    openapi_url=None if is_prod else "/open_api.json",
)


class _PlaygroundCors:
    """CORS for the web UI's chat tester. The origins live in ai-agent-config, which is only read at startup."""

    def __init__(self, app) -> None:
        self.app = app
        self.origins = None
        self.cors = None

    async def __call__(self, scope, receive, send):
        origins = getattr(scope["app"].state, "cors_origins", None) if scope["type"] == "http" else None
        if not origins:
            return await self.app(scope, receive, send)
        if origins != self.origins:  # first request, or a restart with another config
            self.origins = origins
            self.cors = CORSMiddleware(self.app, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])
        return await self.cors(scope, receive, send)


app.add_middleware(_PlaygroundCors)


@app.exception_handler(LLMError)
async def llm_error_handler(_: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_body(exc))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    location = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    message = f"{location}: {first.get('msg')}" if location else str(first.get("msg", "Invalid request"))
    return JSONResponse(
        status_code=422,
        content={"error": {"message": message, "type": "invalid_request_error", "code": "validation_error"}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "type": "api_error", "code": "internal_error"}},
    )


@app.get("/health", tags=["system"])
async def health(request: Request) -> dict:
    service = request.app.state.chat_service
    rag = "off" if service.retriever is None else ("ready" if service.retriever.ready else "unavailable")
    customers = "off" if service.customers is None else ("ready" if service.customers.ready else "unavailable")
    return {
        "status": "ok", "env": config.ENV.value, "models": [m.id for m in service.manager.models], "rag": rag,
        "customer_records": customers,
    }


app.include_router(api_v1_router, prefix=config.API_V1_STR)
# No API key: the browser calls it directly. Off unless playground.enabled.
app.include_router(playground.router, prefix=config.API_V1_STR, tags=["playground"])

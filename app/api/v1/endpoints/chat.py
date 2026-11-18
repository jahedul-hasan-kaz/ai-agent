from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_chat_service
from app.models.chat import (
    AgentInfo,
    AgentListResponse,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelInfo,
    ModelListResponse,
    ServerModelsInfo,
    ServerModelsResponse,
)
from app.services.agent_prompts import DEFAULT_AGENT, agents
from app.services.chat_service import ChatService
from app.tools.registry import agent_tools

router = APIRouter()


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "JSON, or SSE chunks when stream=true"},
        400: {"description": "Missing or unknown model, model_type mismatch, or parameters the model rejected"},
        401: {"description": "Missing or invalid API key"},
        429: {"description": "Upstream rate limit"},
        502: {"description": "The model and its fallbacks failed upstream"},
        503: {"description": "Model server unreachable"},
        504: {"description": "Model timed out"},
    },
)
async def create_chat_completion(body: ChatCompletionRequest, service: ChatService = Depends(get_chat_service)):
    """Generate the assistant's next message with the model named in `model`."""
    if body.stream:
        return StreamingResponse(
            service.stream(body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await service.complete(body)


@router.get("/models", response_model=ModelListResponse)
async def list_models(service: ChatService = Depends(get_chat_service)):
    """Models configured in ai-agent-config (keys never leave the server)."""
    return ModelListResponse(
        data=[ModelInfo(id=m.id, type=m.type, provider=m.provider, model=m.model) for m in service.manager.models]
    )


@router.get("/agents", response_model=AgentListResponse)
async def list_agents():
    """Agents a request can name in `agent`, one per app/prompts/agents/<agent>_system_instr.md."""
    return AgentListResponse(
        data=[
            AgentInfo(
                id=a.id, label=a.label, default=a.id == DEFAULT_AGENT, prompt_tokens=a.tokens,
                tools=[t.name for t in agent_tools(a.id)],
            )
            for a in agents().values()
        ]
    )


@router.get("/models/remote", response_model=ServerModelsResponse)
async def list_remote_models(service: ChatService = Depends(get_chat_service)):
    """What each configured server (base_url) actually serves right now.
    A local Ollama base_url returns only the models installed on that machine."""
    servers = await service.manager.server_models()
    return ServerModelsResponse(data=[ServerModelsInfo(**vars(s)) for s in servers])

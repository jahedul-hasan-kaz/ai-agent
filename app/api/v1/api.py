from fastapi import APIRouter, Depends

from app.api.deps import verify_api_key
from app.api.v1.endpoints import chat, docs

router = APIRouter(dependencies=[Depends(verify_api_key)])
router.include_router(chat.router, tags=["chat"])
router.include_router(docs.router, tags=["docs"])

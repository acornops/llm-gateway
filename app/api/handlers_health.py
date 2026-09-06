from fastapi import APIRouter

from app.config.settings import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "capacity_contract_version": 1,
        "capacity_enabled": settings.WORKSPACE_CAPACITY_ENABLED,
    }

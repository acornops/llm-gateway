import secrets

from fastapi import Header, HTTPException, status

from app.config.settings import settings


async def require_admin_service_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid service token",
        )

    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.ADMIN_API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )

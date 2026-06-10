from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator

from app.auth.service_token import require_admin_service_token
from app.llm.adapters.registry import is_provider_enabled
from app.llm.service import SUPPORTED_LLM_PROVIDERS, normalize_provider_name
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store

router = APIRouter()


class ProviderCredentialStatus(BaseModel):
    provider: str
    configured: bool
    enabled: bool


class ProviderCredentialStatusResponse(BaseModel):
    workspace_id: str
    providers: list[ProviderCredentialStatus]


class ProviderCredentialUpsertRequest(BaseModel):
    workspace_id: str = Field(min_length=1)
    api_key: str = Field(min_length=1)

    @field_validator("workspace_id", "api_key")
    @classmethod
    def trim_non_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


def _provider_or_404(provider: str) -> str:
    normalized = normalize_provider_name(provider)
    if normalized not in SUPPORTED_LLM_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unsupported provider")
    return normalized


def _secret_name(provider: str) -> str:
    return f"{provider}_api_key"


def _workspace_id_or_422(workspace_id: str) -> str:
    normalized = workspace_id.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="workspace_id must not be blank")
    return normalized


async def _is_configured(provider: str, workspace_id: str) -> bool:
    try:
        value = await secret_store.get_secret(
            _secret_name(provider),
            {"workspace_id": workspace_id},
        )
        return bool(value and value.strip())
    except SecretNotFoundError:
        return False


@router.get("/provider-credentials", response_model=ProviderCredentialStatusResponse)
async def list_provider_credentials(
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> ProviderCredentialStatusResponse:
    workspace_id = _workspace_id_or_422(workspace_id)
    providers: list[ProviderCredentialStatus] = []
    for provider in SUPPORTED_LLM_PROVIDERS:
        providers.append(
            ProviderCredentialStatus(
                provider=provider,
                configured=await _is_configured(provider, workspace_id),
                enabled=is_provider_enabled(provider),
            )
        )
    return ProviderCredentialStatusResponse(workspace_id=workspace_id, providers=providers)


@router.put("/provider-credentials/{provider}", response_model=ProviderCredentialStatus)
async def put_provider_credential(
    request: ProviderCredentialUpsertRequest,
    provider: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> ProviderCredentialStatus:
    normalized = _provider_or_404(provider)
    await secret_store.put_secret(
        _secret_name(normalized),
        request.api_key,
        {"workspace_id": request.workspace_id},
    )
    return ProviderCredentialStatus(
        provider=normalized,
        configured=True,
        enabled=is_provider_enabled(normalized),
    )


@router.delete("/provider-credentials/{provider}", response_model=ProviderCredentialStatus)
async def delete_provider_credential(
    provider: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> ProviderCredentialStatus:
    normalized = _provider_or_404(provider)
    workspace_id = _workspace_id_or_422(workspace_id)
    await secret_store.delete_secret(_secret_name(normalized), {"workspace_id": workspace_id})
    return ProviderCredentialStatus(
        provider=normalized,
        configured=False,
        enabled=is_provider_enabled(normalized),
    )

from dataclasses import dataclass

import jwt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.claims import TokenClaims
from app.auth.jwks import jwks_manager
from app.config.settings import settings
from app.observability.metrics import GATEWAY_JWT_VALIDATIONS_TOTAL

security = HTTPBearer()
logger = structlog.get_logger()


@dataclass(frozen=True)
class TokenContext:
    token: str
    claims: TokenClaims


class JwtValidator:
    """
    Validates JWT tokens against a JWKS endpoint.
    """

    async def validate(
        self, credentials: HTTPAuthorizationCredentials = Depends(security)
    ) -> TokenClaims:
        """
        Extracts and validates a token from the request.
        """
        return await self.validate_token(credentials.credentials)

    async def validate_token(self, token: str) -> TokenClaims:
        """
        Validates a raw bearer token and returns its claims.
        """
        try:
            signing_key = await jwks_manager.get_signing_key(token)
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=settings.AUTH_AUDIENCE,
                issuer=settings.AUTH_ISSUER,
                leeway=settings.AUTH_CLOCK_SKEW_SEC,
            )
            GATEWAY_JWT_VALIDATIONS_TOTAL.labels(status="success").inc()
            return TokenClaims(**payload)
        except jwt.PyJWTError as e:
            GATEWAY_JWT_VALIDATIONS_TOTAL.labels(status="failure").inc()
            logger.warning(
                "jwt_validation_failed",
                error_type=type(e).__name__,
                error=str(e),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            ) from e
        except Exception as e:
            GATEWAY_JWT_VALIDATIONS_TOTAL.labels(status="failure").inc()
            logger.warning(
                "jwt_validation_failed",
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            ) from e


validator = JwtValidator()


async def get_current_claims(
    claims: TokenClaims = Depends(validator.validate),
) -> TokenClaims:
    return claims


async def get_current_token_context(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    claims: TokenClaims = Depends(validator.validate),
) -> TokenContext:
    return TokenContext(token=credentials.credentials, claims=claims)

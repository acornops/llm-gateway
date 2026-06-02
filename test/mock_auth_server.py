import base64
import time

import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock Auth Service")

# Generate a persistent key pair
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()

_private_pem = _private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)


@app.get("/jwks.json")
async def jwks():
    numbers = _public_key.public_numbers()

    def to_base64url(n):
        return (
            base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big"))
            .decode("utf-8")
            .rstrip("=")
        )

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": "mock-kid",
                "n": to_base64url(numbers.n),
                "e": to_base64url(numbers.e),
                "alg": "RS256",
            }
        ]
    }


class TokenRequest(BaseModel):
    run_id: str
    workspace_id: str
    target_id: str
    session_id: str
    sub: str = "test-user"


@app.post("/token")
async def generate_token(req: TokenRequest):
    payload = {
        "iss": "llm-gateway",
        "aud": "execution-gateway",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "sub": req.sub,
        "run_id": req.run_id,
        "workspace_id": req.workspace_id,
        "target_id": req.target_id,
        "session_id": req.session_id,
        "permissions": {
            "allowed_providers": ["openai", "anthropic", "gemini"],
            "allowed_tools": ["*"],
            "max_output_tokens": 4096,
        },
    }
    token = jwt.encode(payload, _private_pem, algorithm="RS256", headers={"kid": "mock-kid"})
    return {"access_token": token, "token_type": "bearer"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)

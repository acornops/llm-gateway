import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config.settings import settings


class Crypto:
    """
    Encryption utility using AES-256-GCM.
    Supports Associated Data (AAD) for context-bound decryption.
    """

    def __init__(self, kek_base64: str):
        self.kek = base64.b64decode(kek_base64)
        if len(self.kek) != 32:
            raise ValueError("KEK must be 32 bytes (base64 encoded 44 chars)")

    def encrypt(self, plaintext: str, aad: bytes) -> tuple[bytes, bytes]:
        """
        Encrypts plaintext with AAD. Returns (ciphertext, nonce).
        """
        aesgcm = AESGCM(self.kek)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), aad)
        return ciphertext, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes, aad: bytes) -> str:
        """
        Decrypts ciphertext with AAD.
        """
        aesgcm = AESGCM(self.kek)
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
        return plaintext.decode()


crypto = Crypto(settings.SECRETS_KEK_BASE64)

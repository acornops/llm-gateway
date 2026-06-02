import base64

import pytest
from cryptography.exceptions import InvalidTag

from app.secrets.crypto import Crypto


def _kek(size: int = 32) -> str:
    return base64.b64encode(bytes([7]) * size).decode()


def test_crypto_rejects_non_256_bit_keys():
    with pytest.raises(ValueError, match="KEK must be 32 bytes"):
        Crypto(_kek(31))


def test_crypto_encrypt_decrypt_round_trip():
    crypto = Crypto(_kek())

    ciphertext, nonce = crypto.encrypt("secret-value", b"context")

    assert crypto.decrypt(ciphertext, nonce, b"context") == "secret-value"


def test_crypto_rejects_wrong_associated_data():
    crypto = Crypto(_kek())
    ciphertext, nonce = crypto.encrypt("secret-value", b"context")

    with pytest.raises(InvalidTag):
        crypto.decrypt(ciphertext, nonce, b"other-context")

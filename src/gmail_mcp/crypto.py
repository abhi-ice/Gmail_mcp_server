"""
Symmetric encryption for OAuth tokens at rest.

Why: refresh tokens are long-lived bearer credentials for Google APIs. Storing
them as plain JSON on a shared VPS is a footgun. Fernet (AES-128-CBC + HMAC)
encrypts them with a key loaded from env.
"""

import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .config import get_encryption_key


def encrypt(plaintext: bytes) -> bytes:
    f = Fernet(get_encryption_key())
    return f.encrypt(plaintext)


def decrypt(ciphertext: bytes) -> bytes:
    f = Fernet(get_encryption_key())
    try:
        return f.decrypt(ciphertext)
    except InvalidToken as e:
        raise RuntimeError(
            "Could not decrypt stored token. The GMAIL_MCP_ENCRYPTION_KEY may "
            "have changed since this token was stored."
        ) from e


# ----- Bearer tokens -----

BEARER_TOKEN_PREFIX = "gmcp_"
BEARER_TOKEN_BYTES = 24  # → 48 hex chars after prefix; very high entropy


def generate_bearer_token() -> str:
    return BEARER_TOKEN_PREFIX + secrets.token_hex(BEARER_TOKEN_BYTES)


def hash_bearer_token(token: str) -> str:
    """SHA256 hash for DB storage. Hash is what we compare against on each request."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token_prefix(token: str) -> str:
    """First 12 chars — for display/debug only, never used for auth."""
    return token[:12]

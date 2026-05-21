"""
Environment-driven configuration for the Gmail MCP server.

All paths and secrets come from env vars (loaded via python-dotenv if a .env
file is present). This module is the single source of truth — no other module
should call os.environ directly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env from cwd if present (no-op otherwise)
load_dotenv()


def _get(key: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(key, default)
    if val is None or val == "":
        return None
    return val


def _get_required(key: str) -> str:
    val = _get(key)
    if val is None:
        raise RuntimeError(
            f"Required environment variable {key} is not set. "
            f"See .env.example for the full list of required vars."
        )
    return val


# ----- Storage -----

DATA_DIR = Path(_get("GMAIL_MCP_DATA_DIR", str(Path.home() / ".gmail-mcp"))).expanduser().resolve()
DB_PATH = DATA_DIR / "data.db"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except (OSError, NotImplementedError):
        # Windows / non-POSIX — chmod is best-effort
        pass


# ----- Encryption -----

def get_encryption_key() -> bytes:
    """
    Fernet key used to encrypt OAuth refresh tokens at rest.
    Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    """
    key = _get_required("GMAIL_MCP_ENCRYPTION_KEY")
    return key.encode("utf-8")


# ----- Google OAuth client -----

def get_oauth_client_id() -> str:
    return _get_required("GMAIL_MCP_OAUTH_CLIENT_ID")


def get_oauth_client_secret() -> str:
    return _get_required("GMAIL_MCP_OAUTH_CLIENT_SECRET")


def get_oauth_redirect_uri() -> str:
    """
    Public URL that Google redirects back to after consent.
    Must match exactly what's registered in Google Cloud Console.
    Example: https://gmail-mcp.example.com/oauth/callback
    """
    return _get_required("GMAIL_MCP_OAUTH_REDIRECT_URI")


# ----- Transport -----

TRANSPORT = (_get("GMAIL_MCP_TRANSPORT", "stdio") or "stdio").lower()
# "stdio" — local single-user dev mode (no bearer auth required; uses a "local-dev" implicit user)
# "http"  — remote multi-tenant mode (bearer auth required on every request)

HTTP_HOST = _get("GMAIL_MCP_HTTP_HOST", "0.0.0.0") or "0.0.0.0"
HTTP_PORT = int(_get("GMAIL_MCP_HTTP_PORT", "8765") or "8765")

# Public base URL the server tells users to visit for OAuth (e.g., https://gmail-mcp.example.com)
def get_public_base_url() -> str:
    if TRANSPORT == "stdio":
        # Local OAuth flow uses localhost callback handled by InstalledAppFlow
        return f"http://localhost:{HTTP_PORT}"
    return _get_required("GMAIL_MCP_PUBLIC_BASE_URL").rstrip("/")


# ----- Scopes -----

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/contacts.readonly",
]


# ----- OAuth state token TTL -----

OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes to complete the consent flow


# ----- Logging helper -----

def log(msg: str) -> None:
    print(f"[gmail-mcp] {msg}", file=sys.stderr)

"""
OAuth2 token manager for multi-account Gmail MCP server.

Handles:
- Per-account token storage at ~/.gmail-mcp/accounts/{alias}/token.json
- Auto-refresh of expired tokens
- Caching of Google API service objects
- Thread-safe config reads/writes via atomic file replacement
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request  # type: ignore
from google.oauth2.credentials import Credentials  # type: ignore
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
from googleapiclient.discovery import build  # type: ignore

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/contacts.readonly",
]

_BASE_DIR = Path.home() / ".gmail-mcp"
_OAUTH_KEYS_PATH = _BASE_DIR / "oauth-keys.json"
_CONFIG_PATH = _BASE_DIR / "config.json"
_ACCOUNTS_DIR = _BASE_DIR / "accounts"

# In-memory caches
_service_cache: dict[str, Any] = {}          # alias → gmail service
_people_service_cache: dict[str, Any] = {}   # alias → people service
_label_cache: dict[str, dict] = {}           # alias → {"labels": [...], "fetched_at": float}
_cache_lock = threading.Lock()

LABEL_CACHE_TTL = 300  # seconds (5 minutes)


def _ensure_dirs() -> None:
    """Create required directories if they don't exist."""
    _BASE_DIR.mkdir(mode=0o700, exist_ok=True)
    _ACCOUNTS_DIR.mkdir(mode=0o700, exist_ok=True)


def _token_path(alias: str) -> Path:
    return _ACCOUNTS_DIR / alias / "token.json"


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically using a temp file + rename, so partial writes never corrupt data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_config_lock = threading.Lock()


def load_config() -> dict:
    """Load account config from disk. Returns empty config if file doesn't exist."""
    _ensure_dirs()
    if not _CONFIG_PATH.exists():
        return {"accounts": {}}
    with _config_lock:
        try:
            with open(_CONFIG_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[gmail-mcp] Warning: could not read config.json: {e}", file=sys.stderr)
            return {"accounts": {}}


def save_config(config: dict) -> None:
    """Save account config to disk atomically."""
    _ensure_dirs()
    with _config_lock:
        _write_json_atomic(_CONFIG_PATH, config)


def get_credentials(alias: str) -> Credentials | None:
    """
    Load credentials for an account alias, refreshing if expired.
    Returns None if no token exists.
    Raises RuntimeError if refresh fails (token revoked).
    """
    path = _token_path(alias)
    if not path.exists():
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except Exception as e:
        print(f"[gmail-mcp] Could not load token for '{alias}': {e}", file=sys.stderr)
        return None

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _write_json_atomic(path, json.loads(creds.to_json()))
            print(f"[gmail-mcp] Token refreshed for account '{alias}'", file=sys.stderr)
            return creds
        except Exception as e:
            print(f"[gmail-mcp] Token refresh failed for '{alias}': {e}", file=sys.stderr)
            raise RuntimeError(
                f"Token refresh failed for account '{alias}'. "
                f"Please re-authenticate using: gmail_authenticate(alias='{alias}', email='...')"
            ) from e

    return None


def authenticate_account(alias: str, email: str, description: Optional[str] = None) -> str:
    """
    Run the OAuth2 installed-app flow for a Gmail account.
    Opens a browser window for Google sign-in.
    Saves token and updates config on success.
    Returns success message string.
    """
    _ensure_dirs()

    if not _OAUTH_KEYS_PATH.exists():
        raise FileNotFoundError(
            f"OAuth keys file not found at {_OAUTH_KEYS_PATH}. "
            "Please download your OAuth 2.0 Client ID credentials from Google Cloud Console "
            "and save them as ~/.gmail-mcp/oauth-keys.json. "
            "See the README for detailed setup instructions."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(_OAUTH_KEYS_PATH),
            scopes=SCOPES,
        )
        creds = flow.run_local_server(port=0)
    except Exception as e:
        raise RuntimeError(f"OAuth flow failed: {e}") from e

    # Save token
    token_path = _token_path(alias)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(token_path, json.loads(creds.to_json()))
    print(f"[gmail-mcp] Token saved for account '{alias}' at {token_path}", file=sys.stderr)

    # Update config
    config = load_config()
    config.setdefault("accounts", {})[alias] = {
        "email": email,
        "description": description or "",
    }
    save_config(config)

    # Invalidate service cache for this alias
    with _cache_lock:
        _service_cache.pop(alias, None)
        _people_service_cache.pop(alias, None)
        _label_cache.pop(alias, None)

    return f"Successfully authenticated account '{alias}' ({email})."


def get_gmail_service(alias: str):
    """
    Return a cached Gmail API service object for the given account alias.
    Builds a new one if not cached or if credentials need refreshing.
    """
    with _cache_lock:
        svc = _service_cache.get(alias)
        if svc is not None:
            return svc

    creds = get_credentials(alias)
    if creds is None:
        raise RuntimeError(
            f"No credentials found for account '{alias}'. "
            f"Use gmail_authenticate to authenticate this account first."
        )

    service = build("gmail", "v1", credentials=creds)
    with _cache_lock:
        _service_cache[alias] = service
    return service


def get_people_service(alias: str):
    """
    Return a cached Google People API service object for the given account alias.
    """
    with _cache_lock:
        svc = _people_service_cache.get(alias)
        if svc is not None:
            return svc

    creds = get_credentials(alias)
    if creds is None:
        raise RuntimeError(
            f"No credentials found for account '{alias}'. "
            f"Use gmail_authenticate to authenticate this account first."
        )

    service = build("people", "v1", credentials=creds)
    with _cache_lock:
        _people_service_cache[alias] = service
    return service


def get_label_cache(alias: str, force_refresh: bool = False) -> list[dict]:
    """
    Return the cached label list for an account. Fetches from API if stale or missing.
    Cache TTL: 5 minutes.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _label_cache.get(alias)
        if cached and not force_refresh:
            age = now - cached["fetched_at"]
            if age < LABEL_CACHE_TTL:
                return cached["labels"]

    # Fetch fresh labels
    service = get_gmail_service(alias)
    result = service.users().labels().list(userId="me").execute()
    labels = result.get("labels", [])

    with _cache_lock:
        _label_cache[alias] = {"labels": labels, "fetched_at": time.monotonic()}

    return labels


def invalidate_label_cache(alias: str) -> None:
    """Force-invalidate the label cache for an account."""
    with _cache_lock:
        _label_cache.pop(alias, None)


def resolve_label_ids_to_names(alias: str, label_ids: list[str]) -> list[str]:
    """Convert a list of label IDs to human-readable label names."""
    labels = get_label_cache(alias)
    id_to_name = {lbl["id"]: lbl["name"] for lbl in labels}
    return [id_to_name.get(lid, lid) for lid in label_ids]


def resolve_label_names_to_ids(alias: str, label_names: list[str]) -> list[str]:
    """
    Convert label names to IDs.
    If a value already looks like an ID (all uppercase or starts with 'Label_'), pass through.
    """
    labels = get_label_cache(alias)
    name_to_id = {lbl["name"].lower(): lbl["id"] for lbl in labels}
    result = []
    for name in label_names:
        # System labels are already their own IDs
        if name.upper() in {
            "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED", "IMPORTANT",
            "UNREAD", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
            "CATEGORY_UPDATES", "CATEGORY_FORUMS",
        }:
            result.append(name.upper())
        elif name.startswith("Label_"):
            result.append(name)
        else:
            resolved = name_to_id.get(name.lower())
            if resolved:
                result.append(resolved)
            else:
                # Pass through as-is and let the API reject it if invalid
                result.append(name)
    return result


def check_auth_status(alias: str) -> dict:
    """
    Check if an account has valid credentials.
    Returns dict with 'authenticated' bool and 'status' string.
    """
    path = _token_path(alias)
    if not path.exists():
        return {"authenticated": False, "status": "No token file found"}

    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except Exception:
        return {"authenticated": False, "status": "Token file is invalid or corrupt"}

    if creds.valid:
        return {"authenticated": True, "status": "Authenticated"}
    if creds.expired and creds.refresh_token:
        return {"authenticated": True, "status": "Token expired (will auto-refresh on next use)"}
    return {"authenticated": False, "status": "Token expired and no refresh token — re-authentication required"}

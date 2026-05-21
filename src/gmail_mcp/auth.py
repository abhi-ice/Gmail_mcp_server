"""
OAuth2 + Google API service builders for the multi-tenant Gmail MCP server.

Per-user, per-account tokens live in SQLite (encrypted). On token refresh we
write the new access token back to the DB so we don't keep refreshing on
every request.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional
from urllib.parse import urlencode

from google.auth.transport.requests import Request  # type: ignore
from google.oauth2.credentials import Credentials  # type: ignore
from googleapiclient.discovery import build  # type: ignore

from . import storage
from .config import (
    SCOPES,
    get_oauth_client_id,
    get_oauth_client_secret,
    get_oauth_redirect_uri,
    log,
)

# In-memory service caches keyed by (user_id, alias). LRU not strictly needed for 20 users.
_service_cache: dict[tuple[str, str], Any] = {}
_people_service_cache: dict[tuple[str, str], Any] = {}
_label_cache: dict[tuple[str, str], dict] = {}
_cache_lock = threading.Lock()

LABEL_CACHE_TTL = 300  # seconds


# ---------------------------------------------------------------------------
# OAuth: building the consent URL
# ---------------------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def build_consent_url(state: str) -> str:
    """
    Build the Google OAuth consent URL. The user clicks this; Google calls
    our /oauth/callback with the code on success.
    """
    params = {
        "client_id": get_oauth_client_id(),
        "redirect_uri": get_oauth_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # force refresh_token to be issued every time
        "state": state,
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """
    Exchange an authorization code for tokens. Returns a dict matching what
    google.oauth2.credentials.Credentials.to_json() produces (so existing code
    that builds Credentials from this dict keeps working).
    """
    import httpx  # local import to keep cold start light

    resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": get_oauth_client_id(),
            "client_secret": get_oauth_client_secret(),
            "redirect_uri": get_oauth_redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()

    # Normalise to the schema google-auth's Credentials expects
    token_data = {
        "token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "token_uri": GOOGLE_TOKEN_URL,
        "client_id": get_oauth_client_id(),
        "client_secret": get_oauth_client_secret(),
        "scopes": SCOPES,
        "expiry": _expiry_iso(payload.get("expires_in", 3600)),
    }
    if not token_data["refresh_token"]:
        raise RuntimeError(
            "Google did not return a refresh_token. This usually means the user has "
            "already granted access to this client — revoke it at "
            "https://myaccount.google.com/permissions and try again."
        )
    return token_data


def _expiry_iso(expires_in_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).isoformat()


# ---------------------------------------------------------------------------
# Service builders
# ---------------------------------------------------------------------------

def _credentials_from_token_dict(token_data: dict) -> Credentials:
    """Build a google-auth Credentials from a stored token dict."""
    return Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", GOOGLE_TOKEN_URL),
        client_id=token_data.get("client_id", get_oauth_client_id()),
        client_secret=token_data.get("client_secret", get_oauth_client_secret()),
        scopes=token_data.get("scopes", SCOPES),
    )


def _load_or_refresh(user_id: str, alias: str) -> Optional[Credentials]:
    token_data = storage.get_account_token(user_id, alias)
    if not token_data:
        return None

    creds = _credentials_from_token_dict(token_data)
    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            log(f"Token refresh failed for user={user_id} alias={alias}: {e}")
            raise RuntimeError(
                f"Token refresh failed for account '{alias}'. "
                f"Please re-authenticate using gmail_authenticate."
            ) from e
        # Persist refreshed token
        updated = json.loads(creds.to_json())
        # Preserve client_id/secret (creds.to_json() omits the secret)
        updated["client_secret"] = get_oauth_client_secret()
        storage.update_account_token(user_id, alias, updated)
        return creds

    return None


def get_gmail_service(user_id: str, alias: str):
    """Return a Gmail API service for (user, alias). Raises if not authenticated."""
    key = (user_id, alias)
    with _cache_lock:
        svc = _service_cache.get(key)
        if svc is not None:
            return svc

    creds = _load_or_refresh(user_id, alias)
    if creds is None:
        raise RuntimeError(
            f"No credentials found for account '{alias}'. "
            f"Use gmail_authenticate to authenticate this account first."
        )

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    with _cache_lock:
        _service_cache[key] = service
    return service


def get_people_service(user_id: str, alias: str):
    key = (user_id, alias)
    with _cache_lock:
        svc = _people_service_cache.get(key)
        if svc is not None:
            return svc

    creds = _load_or_refresh(user_id, alias)
    if creds is None:
        raise RuntimeError(
            f"No credentials found for account '{alias}'. "
            f"Use gmail_authenticate to authenticate this account first."
        )

    service = build("people", "v1", credentials=creds, cache_discovery=False)
    with _cache_lock:
        _people_service_cache[key] = service
    return service


def invalidate_caches(user_id: str, alias: str) -> None:
    key = (user_id, alias)
    with _cache_lock:
        _service_cache.pop(key, None)
        _people_service_cache.pop(key, None)
        _label_cache.pop(key, None)


# ---------------------------------------------------------------------------
# Label cache (per user+alias)
# ---------------------------------------------------------------------------

def get_label_cache(user_id: str, alias: str, force_refresh: bool = False) -> list[dict]:
    key = (user_id, alias)
    now = time.monotonic()
    with _cache_lock:
        cached = _label_cache.get(key)
        if cached and not force_refresh:
            if now - cached["fetched_at"] < LABEL_CACHE_TTL:
                return cached["labels"]

    service = get_gmail_service(user_id, alias)
    result = service.users().labels().list(userId="me").execute()
    labels = result.get("labels", [])

    with _cache_lock:
        _label_cache[key] = {"labels": labels, "fetched_at": time.monotonic()}
    return labels


def resolve_label_ids_to_names(user_id: str, alias: str, label_ids: list[str]) -> list[str]:
    labels = get_label_cache(user_id, alias)
    id_to_name = {lbl["id"]: lbl["name"] for lbl in labels}
    return [id_to_name.get(lid, lid) for lid in label_ids]


def resolve_label_names_to_ids(user_id: str, alias: str, label_names: list[str]) -> list[str]:
    labels = get_label_cache(user_id, alias)
    name_to_id = {lbl["name"].lower(): lbl["id"] for lbl in labels}
    result = []
    SYSTEM = {
        "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED", "IMPORTANT",
        "UNREAD", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES", "CATEGORY_FORUMS",
    }
    for name in label_names:
        if name.upper() in SYSTEM:
            result.append(name.upper())
        elif name.startswith("Label_"):
            result.append(name)
        else:
            resolved = name_to_id.get(name.lower())
            result.append(resolved if resolved else name)
    return result


# ---------------------------------------------------------------------------
# Initiating the OAuth flow (called by the gmail_authenticate tool)
# ---------------------------------------------------------------------------

def start_oauth_flow(user_id: str, alias: str, email: str, description: Optional[str]) -> str:
    """
    Create a state token and return the Google consent URL the user should click.
    """
    state = storage.create_oauth_state(user_id, alias, email, description)
    return build_consent_url(state)


def complete_oauth_flow(state: str, code: str) -> dict:
    """
    Called by the /oauth/callback HTTP handler. Returns the (alias, email) that
    was just authenticated.
    """
    ctx = storage.consume_oauth_state(state)
    if ctx is None:
        raise RuntimeError("OAuth state token is invalid or has expired. Please start over.")

    token_data = exchange_code_for_tokens(code)
    storage.save_account(
        user_id=ctx["user_id"],
        alias=ctx["alias"],
        email=ctx["email"],
        description=ctx["description"],
        token_json=token_data,
    )
    invalidate_caches(ctx["user_id"], ctx["alias"])
    log(f"OAuth complete for user={ctx['user_id']} alias={ctx['alias']}")
    return ctx


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------

def check_auth_status(user_id: str, alias: str) -> dict:
    token_data = storage.get_account_token(user_id, alias)
    if not token_data:
        return {"authenticated": False, "status": "Not authenticated"}
    try:
        creds = _credentials_from_token_dict(token_data)
    except Exception:
        return {"authenticated": False, "status": "Stored token is invalid"}

    if creds.valid:
        return {"authenticated": True, "status": "Authenticated"}
    if creds.expired and creds.refresh_token:
        return {"authenticated": True, "status": "Token expired (auto-refresh on next use)"}
    return {"authenticated": False, "status": "Re-authentication required"}

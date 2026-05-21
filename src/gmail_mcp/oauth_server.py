"""
OAuth 2.1 Authorization Server endpoints for the Gmail MCP server.

This module implements the spec endpoints that let MCP clients (Claude Desktop,
mcp-remote, etc.) sign users in WITHOUT a pre-issued bearer token.

Flow (caller pastes config snippet with no token):

  1. Claude Desktop hits /mcp        -> 401 + WWW-Authenticate metadata
  2. Claude Desktop reads /.well-known/oauth-protected-resource (or AS metadata)
  3. Claude Desktop POSTs /oauth/register  (Dynamic Client Registration)
  4. Claude Desktop redirects user to /oauth/authorize (with PKCE)
  5. Our /authorize -> redirect to Google (we proxy identity)
  6. Google /oauth2/auth -> user logs in, consents to Gmail scopes
  7. Google -> /oauth/google-callback?code=...&state=...
  8. We exchange code with Google, get email + Gmail tokens
  9. We find_or_create user by google_sub; auto-connect Gmail as alias 'primary'
  10. We issue our OWN authorization code, redirect to Claude Desktop callback
  11. Claude Desktop POSTs /oauth/token (with PKCE verifier) -> our access token
  12. All subsequent /mcp requests carry our access token

References:
  - RFC 6749  (OAuth 2.0 core)
  - RFC 7636  (PKCE)
  - RFC 7591  (Dynamic Client Registration)
  - RFC 8414  (Authorization Server Metadata)
  - RFC 9728  (Protected Resource Metadata)
  - MCP spec authorization section
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import storage
from .config import (
    SCOPES,
    email_is_allowed,
    get_oauth_client_id,
    get_oauth_client_secret,
    get_oauth_redirect_uri,
    get_public_base_url,
    log,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


# ---------------------------------------------------------------------------
# Discovery metadata
# ---------------------------------------------------------------------------

async def authorization_server_metadata(_request: Request) -> JSONResponse:
    """RFC 8414 — Authorization Server Metadata."""
    base = get_public_base_url()
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": ["gmail"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def protected_resource_metadata(_request: Request) -> JSONResponse:
    """RFC 9728 — Protected Resource Metadata (used by MCP for discovery)."""
    base = get_public_base_url()
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["gmail"],
    })


# ---------------------------------------------------------------------------
# Dynamic Client Registration
# ---------------------------------------------------------------------------

async def register(request: Request):
    """RFC 7591 — minimal Dynamic Client Registration. Public clients only (no secret)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "Body must be JSON."}, status_code=400)

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris is required and non-empty."},
            status_code=400,
        )
    for uri in redirect_uris:
        if not isinstance(uri, str) or not (uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1") or uri.startswith("https://")):
            return JSONResponse(
                {"error": "invalid_redirect_uri", "error_description": f"Invalid redirect URI: {uri}"},
                status_code=400,
            )

    client_name = (body.get("client_name") or "Unknown MCP client")[:200]
    client_id = storage.register_oauth_client(client_name=client_name, redirect_uris=redirect_uris)

    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": 0,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# /oauth/authorize — entry point for the OAuth dance
# ---------------------------------------------------------------------------

async def authorize(request: Request):
    """
    The MCP client sends the user here. We hand off to Google for identity +
    Gmail scopes, preserving the client's PKCE params in our state token.
    """
    q = request.query_params

    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    response_type = q.get("response_type", "")
    code_challenge = q.get("code_challenge", "")
    code_challenge_method = (q.get("code_challenge_method") or "S256").upper()
    client_state = q.get("state", "")
    scope = q.get("scope", "gmail")

    # ----- Validate request -----
    if response_type != "code":
        return _err_html("Unsupported response_type. Only 'code' is supported.")
    if not client_id:
        return _err_html("Missing client_id.")
    if not redirect_uri:
        return _err_html("Missing redirect_uri.")
    if not code_challenge:
        return _err_html("PKCE code_challenge is required.")
    if code_challenge_method != "S256":
        return _err_html("Only S256 code_challenge_method is supported.")

    client = storage.get_oauth_client(client_id)
    if not client:
        return _err_html("Unknown client_id. Register first via /oauth/register.")
    if redirect_uri not in client["redirect_uris"]:
        return _err_html("redirect_uri does not match a registered redirect URI for this client.")

    # ----- Stash the request and redirect to Google -----
    state = storage.create_signin_state(
        client_id=client_id,
        client_redirect_uri=redirect_uri,
        client_state=client_state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope,
    )

    google_params = {
        "client_id": get_oauth_client_id(),
        "redirect_uri": _google_callback_url(),
        "response_type": "code",
        "scope": " ".join(["openid", "email", "profile"] + SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    }
    return RedirectResponse(url=f"{GOOGLE_AUTH_URL}?{urlencode(google_params)}", status_code=302)


# ---------------------------------------------------------------------------
# /oauth/google-callback — Google → us
# ---------------------------------------------------------------------------

async def google_callback(request: Request):
    """
    Google redirects here after the user signs in. We exchange the code for
    tokens + identity, find_or_create the user, auto-connect their primary
    Gmail, then mint our OWN authorization code and redirect back to the
    MCP client's callback.
    """
    from . import auth  # local import: avoid circular dependency with auth.py
    q = request.query_params
    state = q.get("state", "")
    code = q.get("code", "")
    error = q.get("error", "")

    if error:
        return _err_html(f"Google returned an error: {error}")
    if not state or not code:
        return _err_html("Missing state or code parameter from Google.")

    ctx = storage.consume_signin_state(state)
    if ctx is None:
        return _err_html("Sign-in session expired or invalid. Please retry from your MCP client.")

    # Exchange code with Google
    try:
        token_resp = await _exchange_google_code(code)
    except Exception as e:
        log(f"Google token exchange failed: {e}")
        return _err_html(f"Could not complete Google sign-in: {e}")

    # Get user identity
    id_info = await _userinfo(token_resp["access_token"])
    email = (id_info.get("email") or "").lower().strip()
    google_sub = id_info.get("sub", "")
    if not email or not google_sub:
        return _err_html("Google did not return email + sub.")
    if not id_info.get("email_verified"):
        return _err_html("Google email is not verified.")
    if not email_is_allowed(email):
        return _err_html(f"Sign-in not allowed for {email}. Ask your admin to grant access.")

    # Find or create user, auto-connect primary Gmail
    try:
        user_id = storage.find_or_create_user_by_google(google_sub=google_sub, email=email)
    except PermissionError as e:
        return _err_html(str(e))

    # Save the primary Gmail account if not already present
    existing = storage.get_account(user_id, "primary")
    token_json = _google_token_response_to_credentials_dict(token_resp)
    if existing:
        storage.update_account_token(user_id, "primary", token_json)
    else:
        storage.save_account(
            user_id=user_id, alias="primary", email=email,
            description="Auto-connected on sign-in",
            token_json=token_json,
        )
    auth.invalidate_caches(user_id, "primary")

    # Mint our authorization code, redirect to MCP client's redirect_uri
    our_code = storage.create_authorization_code(
        client_id=ctx["client_id"],
        user_id=user_id,
        redirect_uri=ctx["client_redirect_uri"],
        code_challenge=ctx["code_challenge"],
        code_challenge_method=ctx["code_challenge_method"],
        scope=ctx["scope"],
    )
    params = {"code": our_code}
    if ctx["client_state"]:
        params["state"] = ctx["client_state"]
    redirect_url = f"{ctx['client_redirect_uri']}?{urlencode(params)}"
    log(f"Sign-in complete for {email}; redirecting MCP client back")
    return RedirectResponse(url=redirect_url, status_code=302)


# ---------------------------------------------------------------------------
# /oauth/token — MCP client exchanges authorization code for access token
# ---------------------------------------------------------------------------

async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type", "")
    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type", "error_description": "Only authorization_code is supported."},
            status_code=400,
        )

    code = form.get("code", "")
    client_id = form.get("client_id", "")
    code_verifier = form.get("code_verifier", "")
    redirect_uri = form.get("redirect_uri", "")

    if not code or not client_id or not code_verifier or not redirect_uri:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "code, client_id, redirect_uri, and code_verifier are required."},
            status_code=400,
        )

    record = storage.consume_authorization_code(code)
    if not record:
        return JSONResponse({"error": "invalid_grant", "error_description": "Authorization code is invalid or expired."}, status_code=400)
    if record["client_id"] != client_id:
        return JSONResponse({"error": "invalid_grant", "error_description": "client_id mismatch."}, status_code=400)
    if record["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch."}, status_code=400)

    # Verify PKCE: base64url(SHA256(code_verifier)) == code_challenge
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if computed != record["code_challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed."}, status_code=400)

    access_token = storage.create_access_token(
        user_id=record["user_id"], client_id=client_id, scope=record["scope"],
    )
    from .config import ACCESS_TOKEN_TTL_SECONDS
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "scope": record["scope"] or "gmail",
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# /oauth/revoke — token revocation
# ---------------------------------------------------------------------------

async def revoke(request: Request):
    form = await request.form()
    token_value = form.get("token", "")
    if token_value:
        storage.revoke_access_token(token_value)
    # RFC 7009 — always return 200 regardless
    return JSONResponse({}, status_code=200)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _google_callback_url() -> str:
    base = get_public_base_url().rstrip("/")
    return f"{base}/oauth/google-callback"


async def _exchange_google_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": get_oauth_client_id(),
                "client_secret": get_oauth_client_secret(),
                "redirect_uri": _google_callback_url(),
                "grant_type": "authorization_code",
            },
        )
    resp.raise_for_status()
    payload = resp.json()
    if "refresh_token" not in payload:
        raise RuntimeError(
            "Google did not return a refresh_token. Revoke prior consent at "
            "https://myaccount.google.com/permissions and try again."
        )
    return payload


async def _userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    resp.raise_for_status()
    return resp.json()


def _google_token_response_to_credentials_dict(token_resp: dict) -> dict:
    """Shape a Google token response into the dict the rest of the codebase expects."""
    from datetime import datetime, timedelta, timezone
    return {
        "token": token_resp["access_token"],
        "refresh_token": token_resp.get("refresh_token"),
        "token_uri": GOOGLE_TOKEN_URL,
        "client_id": get_oauth_client_id(),
        "client_secret": get_oauth_client_secret(),
        "scopes": SCOPES,
        "expiry": (datetime.now(timezone.utc) + timedelta(seconds=token_resp.get("expires_in", 3600))).isoformat(),
    }


def _err_html(msg: str, status_code: int = 400) -> HTMLResponse:
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gmail MCP — Error</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#f5f1ea; color:#1a2845;
          display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:white; padding:48px; border-radius:12px; box-shadow:0 4px 24px rgba(0,0,0,0.08);
           max-width:520px; text-align:center; }}
  .icon {{ font-size:64px; color:#c33; line-height:1; margin-bottom:16px; }}
  h1 {{ font-size:20px; margin:0 0 12px; }}
  p {{ color:#555; line-height:1.6; margin:0; }}
</style></head>
<body><div class="card"><div class="icon">✗</div>
<h1>Authentication failed</h1><p>{msg}</p></div></body></html>"""
    return HTMLResponse(body, status_code=status_code)

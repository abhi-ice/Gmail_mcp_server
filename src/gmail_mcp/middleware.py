"""
Starlette middleware: validate the OAuth access token on every MCP request.

On 401, returns a WWW-Authenticate header pointing at our protected-resource
metadata URL so MCP clients (Claude Desktop, mcp-remote) can discover the
authorization server and start the OAuth dance automatically.

Exempt paths:
  - /                              service index
  - /health                        liveness
  - /oauth/*                       OAuth endpoints (have their own validation)
  - /.well-known/*                 discovery metadata
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from . import context, storage
from .config import get_public_base_url

EXEMPT_PREFIXES = (
    "/oauth/",
    "/.well-known/",
    "/health",
)
EXEMPT_EXACT = {"/"}


class OAuthAccessTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in EXEMPT_EXACT or any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)

        auth_header = request.headers.get("authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            return self._challenge("missing_token", "Authorization header missing or malformed.")

        token = auth_header.split(" ", 1)[1].strip()

        # First try OAuth access tokens (the v3 path)
        user = storage.get_user_by_access_token(token)
        if user is None:
            # Legacy fallback: static admin-issued bearer tokens (kept for migration)
            user = storage.get_user_by_token(token)
        if user is None:
            return self._challenge("invalid_token", "The access token is invalid or expired.")

        context.set_current_user(user["id"], user["email"])
        return await call_next(request)

    def _challenge(self, error_code: str, description: str) -> JSONResponse:
        base = get_public_base_url()
        www_auth = (
            f'Bearer realm="gmail-mcp", '
            f'error="{error_code}", '
            f'error_description="{description}", '
            f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
        )
        return JSONResponse(
            {"error": error_code, "error_description": description},
            status_code=401,
            headers={"WWW-Authenticate": www_auth},
        )

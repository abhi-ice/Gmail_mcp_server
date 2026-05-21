"""
Starlette middleware: validate Authorization: Bearer <token> on every MCP request
and inject user_id into the request contextvar so tools can access it.

Exempt paths (no auth required):
  - /oauth/callback    Google calls this; auth is via state token
  - /health            Liveness probe
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from . import context, storage

EXEMPT_PREFIXES = ("/oauth/", "/health")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)

        auth_header = request.headers.get("authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "Missing or malformed Authorization header. Expected: 'Bearer <token>'."},
                status_code=401,
            )
        token = auth_header.split(" ", 1)[1].strip()
        user = storage.get_user_by_token(token)
        if user is None:
            return JSONResponse(
                {"error": "Invalid or revoked bearer token."},
                status_code=401,
            )

        context.set_current_user(user["id"], user["email"])
        return await call_next(request)

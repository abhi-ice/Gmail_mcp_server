"""
Per-request user context.

The middleware sets `current_user_id` after validating a bearer token.
Tool functions read it via require_user_id() — they never accept user_id
as a parameter (callers shouldn't be able to spoof another user).
"""

from contextvars import ContextVar
from typing import Optional

_user_id: ContextVar[Optional[str]] = ContextVar("gmail_mcp_user_id", default=None)
_user_email: ContextVar[Optional[str]] = ContextVar("gmail_mcp_user_email", default=None)


def set_current_user(user_id: str, email: str) -> None:
    _user_id.set(user_id)
    _user_email.set(email)


def get_current_user_id() -> Optional[str]:
    return _user_id.get()


def get_current_user_email() -> Optional[str]:
    return _user_email.get()


def require_user_id() -> str:
    uid = _user_id.get()
    if uid is None:
        raise RuntimeError(
            "No authenticated user in this request. "
            "This is a server bug — the auth middleware should have rejected the request."
        )
    return uid

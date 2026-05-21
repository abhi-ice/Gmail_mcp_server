"""
SQLite-backed storage for users, accounts, and OAuth state tokens.

Schema is created on first run via init_db(). All refresh tokens are stored
encrypted (see crypto.encrypt). Bearer tokens are stored hashed (SHA256) —
the plaintext is shown to the admin once at issuance and never persisted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from .config import DB_PATH, OAUTH_STATE_TTL_SECONDS, ensure_data_dir, log
from .crypto import (
    bearer_token_prefix,
    decrypt,
    encrypt,
    generate_bearer_token,
    hash_bearer_token,
)

_db_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Idempotent."""
    with _db_lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                bearer_token_hash TEXT UNIQUE NOT NULL,
                bearer_token_prefix TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                email TEXT NOT NULL,
                description TEXT,
                token_encrypted BLOB NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                UNIQUE(user_id, alias),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                email TEXT NOT NULL,
                description TEXT,
                expires_at REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_accounts_user
                ON user_accounts(user_id);

            CREATE INDEX IF NOT EXISTS idx_oauth_states_expires
                ON oauth_states(expires_at);
            """
        )


# ----- Users -----

def create_user(email: str, is_admin: bool = False) -> tuple[str, str]:
    """
    Create a new user and issue a bearer token.
    Returns (user_id, plaintext_bearer_token). The plaintext is shown ONCE.
    """
    user_id = str(uuid.uuid4())
    token = generate_bearer_token()
    token_hash = hash_bearer_token(token)
    prefix = bearer_token_prefix(token)

    with _db_lock, _conn() as c:
        c.execute(
            "INSERT INTO users (id, email, bearer_token_hash, bearer_token_prefix, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, email.lower().strip(), token_hash, prefix, 1 if is_admin else 0, _now_iso()),
        )

    log(f"Created user {email} (id={user_id})")
    return user_id, token


def get_user_by_token(token: str) -> Optional[dict]:
    """Validate a bearer token and return the associated user (or None)."""
    token_hash = hash_bearer_token(token)
    with _conn() as c:
        row = c.execute(
            "SELECT id, email, is_admin, revoked_at FROM users WHERE bearer_token_hash = ?",
            (token_hash,),
        ).fetchone()
    if not row:
        return None
    if row["revoked_at"]:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "is_admin": bool(row["is_admin"]),
    }


def get_user_by_email(email: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT id, email, is_admin, bearer_token_prefix, created_at, revoked_at "
            "FROM users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, email, is_admin, bearer_token_prefix, created_at, revoked_at "
            "FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_user(user_id: str) -> bool:
    with _db_lock, _conn() as c:
        cur = c.execute(
            "UPDATE users SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_now_iso(), user_id),
        )
        return cur.rowcount > 0


def rotate_user_token(user_id: str) -> Optional[str]:
    """Issue a new bearer token for a user. Returns the new plaintext, or None if user not found."""
    new_token = generate_bearer_token()
    new_hash = hash_bearer_token(new_token)
    new_prefix = bearer_token_prefix(new_token)
    with _db_lock, _conn() as c:
        cur = c.execute(
            "UPDATE users SET bearer_token_hash = ?, bearer_token_prefix = ? WHERE id = ?",
            (new_hash, new_prefix, user_id),
        )
        if cur.rowcount == 0:
            return None
    return new_token


# ----- Bootstrap: ensure a "local-dev" user exists for stdio mode -----

LOCAL_DEV_USER_ID = "00000000-0000-0000-0000-000000000000"
LOCAL_DEV_EMAIL = "local-dev@gmail-mcp.local"


def ensure_local_dev_user() -> str:
    """For stdio mode: create a fixed local user if missing. Returns the user_id."""
    with _db_lock, _conn() as c:
        row = c.execute("SELECT id FROM users WHERE id = ?", (LOCAL_DEV_USER_ID,)).fetchone()
        if row:
            return LOCAL_DEV_USER_ID
        c.execute(
            "INSERT INTO users (id, email, bearer_token_hash, bearer_token_prefix, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                LOCAL_DEV_USER_ID,
                LOCAL_DEV_EMAIL,
                "local-dev-no-bearer-token",
                "local-dev",
                _now_iso(),
            ),
        )
    log("Created local-dev user (stdio mode)")
    return LOCAL_DEV_USER_ID


# ----- Accounts (Gmail accounts a user has connected) -----

def save_account(
    user_id: str,
    alias: str,
    email: str,
    description: Optional[str],
    token_json: dict,
) -> None:
    """Insert or update a Gmail account for the given user."""
    enc = encrypt(json.dumps(token_json).encode("utf-8"))
    now = _now_iso()
    with _db_lock, _conn() as c:
        existing = c.execute(
            "SELECT id FROM user_accounts WHERE user_id = ? AND alias = ?",
            (user_id, alias),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE user_accounts SET email = ?, description = ?, token_encrypted = ?, last_used_at = ? "
                "WHERE id = ?",
                (email, description or "", enc, now, existing["id"]),
            )
        else:
            c.execute(
                "INSERT INTO user_accounts (id, user_id, alias, email, description, token_encrypted, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), user_id, alias, email, description or "", enc, now),
            )


def get_account_token(user_id: str, alias: str) -> Optional[dict]:
    """Return the decrypted token JSON for (user_id, alias), or None if missing."""
    with _conn() as c:
        row = c.execute(
            "SELECT token_encrypted FROM user_accounts WHERE user_id = ? AND alias = ?",
            (user_id, alias),
        ).fetchone()
    if not row:
        return None
    plaintext = decrypt(row["token_encrypted"])
    return json.loads(plaintext.decode("utf-8"))


def update_account_token(user_id: str, alias: str, token_json: dict) -> None:
    enc = encrypt(json.dumps(token_json).encode("utf-8"))
    with _db_lock, _conn() as c:
        c.execute(
            "UPDATE user_accounts SET token_encrypted = ?, last_used_at = ? "
            "WHERE user_id = ? AND alias = ?",
            (enc, _now_iso(), user_id, alias),
        )


def list_accounts(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT alias, email, description, created_at, last_used_at "
            "FROM user_accounts WHERE user_id = ? ORDER BY alias",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_account(user_id: str, alias: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT alias, email, description, created_at, last_used_at "
            "FROM user_accounts WHERE user_id = ? AND alias = ?",
            (user_id, alias),
        ).fetchone()
    return dict(row) if row else None


def delete_account(user_id: str, alias: str) -> bool:
    with _db_lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM user_accounts WHERE user_id = ? AND alias = ?",
            (user_id, alias),
        )
        return cur.rowcount > 0


# ----- OAuth state tokens (for the remote callback flow) -----

def create_oauth_state(
    user_id: str,
    alias: str,
    email: str,
    description: Optional[str],
) -> str:
    """
    Create a short-lived state token tying together (user, alias, email).
    The state is passed to Google in the OAuth URL; on callback we look it up
    to recover the user context.
    """
    state = generate_bearer_token().replace("gmcp_", "oas_")  # prefix change for clarity
    expires_at = time.time() + OAUTH_STATE_TTL_SECONDS
    with _db_lock, _conn() as c:
        c.execute(
            "INSERT INTO oauth_states (state, user_id, alias, email, description, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state, user_id, alias, email, description or "", expires_at, _now_iso()),
        )
    return state


def consume_oauth_state(state: str) -> Optional[dict]:
    """
    Look up and delete an OAuth state token. Returns the original context
    or None if missing/expired. Single-use.
    """
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT user_id, alias, email, description, expires_at FROM oauth_states WHERE state = ?",
            (state,),
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        if time.time() > row["expires_at"]:
            return None
        return {
            "user_id": row["user_id"],
            "alias": row["alias"],
            "email": row["email"],
            "description": row["description"] or "",
        }


def cleanup_expired_oauth_states() -> int:
    with _db_lock, _conn() as c:
        cur = c.execute("DELETE FROM oauth_states WHERE expires_at < ?", (time.time(),))
        return cur.rowcount

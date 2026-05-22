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
                google_sub TEXT UNIQUE,
                bearer_token_hash TEXT UNIQUE,
                bearer_token_prefix TEXT,
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
                user_id TEXT,
                alias TEXT,
                email TEXT,
                description TEXT,
                kind TEXT NOT NULL DEFAULT 'add_account',
                client_id TEXT,
                client_redirect_uri TEXT,
                client_state TEXT,
                code_challenge TEXT,
                code_challenge_method TEXT,
                scope TEXT,
                expires_at REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_name TEXT,
                redirect_uris TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS oauth_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                code_challenge_method TEXT NOT NULL,
                scope TEXT,
                expires_at REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                token_hash TEXT PRIMARY KEY,
                token_prefix TEXT NOT NULL,
                user_id TEXT NOT NULL,
                client_id TEXT,
                scope TEXT,
                expires_at REAL NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_accounts_user
                ON user_accounts(user_id);
            CREATE INDEX IF NOT EXISTS idx_oauth_states_expires
                ON oauth_states(expires_at);
            CREATE INDEX IF NOT EXISTS idx_oauth_codes_expires
                ON oauth_codes(expires_at);
            CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_user
                ON oauth_access_tokens(user_id);
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
    Look up and delete an add_account OAuth state. Returns the original context
    or None if missing/expired/wrong-kind. Single-use.
    """
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT user_id, alias, email, description, kind, expires_at "
            "FROM oauth_states WHERE state = ?",
            (state,),
        ).fetchone()
        if not row or row["kind"] != "add_account":
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


# ---------------------------------------------------------------------------
# v3 — OAuth authorization server: users by Google identity
# ---------------------------------------------------------------------------

def find_or_create_user_by_google(google_sub: str, email: str) -> str:
    """
    Look up a user by their Google subject ID (stable identifier). If not found,
    look up by email (handles users created before we tracked sub) and link.
    If still not found, create a new user. Returns user_id.
    """
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT id, revoked_at FROM users WHERE google_sub = ?",
            (google_sub,),
        ).fetchone()
        if row:
            if row["revoked_at"]:
                raise PermissionError(f"User {email} is revoked.")
            return row["id"]

        # Try by email (legacy / pre-OAuth users)
        row = c.execute(
            "SELECT id, revoked_at FROM users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
        if row:
            if row["revoked_at"]:
                raise PermissionError(f"User {email} is revoked.")
            c.execute(
                "UPDATE users SET google_sub = ? WHERE id = ?",
                (google_sub, row["id"]),
            )
            return row["id"]

        # Create new user — no bearer token (OAuth-only access)
        user_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO users (id, email, google_sub, is_admin, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (user_id, email.lower().strip(), google_sub, _now_iso()),
        )
    log(f"OAuth sign-in created user {email} (id={user_id})")
    return user_id


# ---------------------------------------------------------------------------
# v3 — OAuth state extension (PKCE + client info)
# ---------------------------------------------------------------------------

def create_signin_state(
    *,
    client_id: str,
    client_redirect_uri: str,
    client_state: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: Optional[str],
) -> str:
    """
    State for the OUTER OAuth dance (MCP client ↔ our server).
    We hand this state to Google; Google returns it to us in the callback.
    """
    state = generate_bearer_token().replace("gmcp_", "sgn_")
    expires_at = time.time() + OAUTH_STATE_TTL_SECONDS
    with _db_lock, _conn() as c:
        c.execute(
            "INSERT INTO oauth_states (state, kind, client_id, client_redirect_uri, "
            "client_state, code_challenge, code_challenge_method, scope, expires_at, created_at) "
            "VALUES (?, 'signin', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state, client_id, client_redirect_uri, client_state,
                code_challenge, code_challenge_method, scope,
                expires_at, _now_iso(),
            ),
        )
    return state


def consume_signin_state(state: str) -> Optional[dict]:
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT kind, client_id, client_redirect_uri, client_state, "
            "code_challenge, code_challenge_method, scope, expires_at "
            "FROM oauth_states WHERE state = ?",
            (state,),
        ).fetchone()
        if not row or row["kind"] != "signin":
            return None
        c.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        if time.time() > row["expires_at"]:
            return None
        return {
            "client_id": row["client_id"],
            "client_redirect_uri": row["client_redirect_uri"],
            "client_state": row["client_state"],
            "code_challenge": row["code_challenge"],
            "code_challenge_method": row["code_challenge_method"],
            "scope": row["scope"],
        }


# ---------------------------------------------------------------------------
# v3 — OAuth clients (Dynamic Client Registration)
# ---------------------------------------------------------------------------

def register_oauth_client(client_name: str, redirect_uris: list[str]) -> str:
    client_id = "mcp_" + uuid.uuid4().hex
    with _db_lock, _conn() as c:
        c.execute(
            "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_at) "
            "VALUES (?, ?, ?, ?)",
            (client_id, client_name, json.dumps(redirect_uris), _now_iso()),
        )
    log(f"Registered OAuth client {client_name} (id={client_id})")
    return client_id


def get_oauth_client(client_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT client_id, client_name, redirect_uris FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "client_id": row["client_id"],
        "client_name": row["client_name"],
        "redirect_uris": json.loads(row["redirect_uris"]),
    }


# ---------------------------------------------------------------------------
# v3 — Authorization codes (one-time, PKCE-bound)
# ---------------------------------------------------------------------------

def create_authorization_code(
    *,
    client_id: str,
    user_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: Optional[str],
) -> str:
    from .config import AUTHORIZATION_CODE_TTL_SECONDS
    code = generate_bearer_token().replace("gmcp_", "cod_")
    expires_at = time.time() + AUTHORIZATION_CODE_TTL_SECONDS
    with _db_lock, _conn() as c:
        c.execute(
            "INSERT INTO oauth_codes (code, client_id, user_id, redirect_uri, "
            "code_challenge, code_challenge_method, scope, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, client_id, user_id, redirect_uri, code_challenge,
             code_challenge_method, scope, expires_at, _now_iso()),
        )
    return code


def consume_authorization_code(code: str) -> Optional[dict]:
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT client_id, user_id, redirect_uri, code_challenge, "
            "code_challenge_method, scope, expires_at FROM oauth_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if not row:
            # Diagnostic: how many codes exist at all? Helps distinguish
            # "code was never written" from "code expired and was swept".
            total = c.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0]
            log(f"consume_authorization_code: code {code[:12]}... NOT FOUND (db has {total} code(s) total)")
            return None
        c.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
        age = time.time() - (row["expires_at"] - 600)  # subtract TTL to get age since creation
        if time.time() > row["expires_at"]:
            log(f"consume_authorization_code: code {code[:12]}... EXPIRED (age={age:.1f}s, TTL was 600s)")
            return None
        log(f"consume_authorization_code: code {code[:12]}... consumed (age={age:.1f}s)")
        return dict(row)


# ---------------------------------------------------------------------------
# v3 — Access tokens (opaque, hashed at rest)
# ---------------------------------------------------------------------------

def create_access_token(
    *, user_id: str, client_id: Optional[str], scope: Optional[str]
) -> str:
    from .config import ACCESS_TOKEN_TTL_SECONDS
    token = generate_bearer_token().replace("gmcp_", "at_")
    token_h = hash_bearer_token(token)
    prefix = bearer_token_prefix(token)
    expires_at = time.time() + ACCESS_TOKEN_TTL_SECONDS
    with _db_lock, _conn() as c:
        c.execute(
            "INSERT INTO oauth_access_tokens (token_hash, token_prefix, user_id, "
            "client_id, scope, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_h, prefix, user_id, client_id, scope, expires_at, _now_iso()),
        )
    return token


def get_user_by_access_token(token: str) -> Optional[dict]:
    token_h = hash_bearer_token(token)
    with _conn() as c:
        row = c.execute(
            "SELECT t.user_id, t.expires_at, t.revoked_at, u.email, u.revoked_at AS user_revoked_at "
            "FROM oauth_access_tokens t JOIN users u ON t.user_id = u.id "
            "WHERE t.token_hash = ?",
            (token_h,),
        ).fetchone()
    if not row:
        return None
    if row["revoked_at"] or row["user_revoked_at"]:
        return None
    if time.time() > row["expires_at"]:
        return None
    return {"id": row["user_id"], "email": row["email"]}


def revoke_access_token(token: str) -> bool:
    token_h = hash_bearer_token(token)
    with _db_lock, _conn() as c:
        cur = c.execute(
            "UPDATE oauth_access_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (_now_iso(), token_h),
        )
        return cur.rowcount > 0


def revoke_all_user_tokens(user_id: str) -> int:
    with _db_lock, _conn() as c:
        cur = c.execute(
            "UPDATE oauth_access_tokens SET revoked_at = ? "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (_now_iso(), user_id),
        )
        return cur.rowcount


def list_user_access_tokens(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT token_prefix, client_id, scope, created_at, expires_at, revoked_at "
            "FROM oauth_access_tokens WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def cleanup_expired_oauth_artifacts() -> dict:
    """Sweep expired states, codes, and tokens. Run periodically (e.g., daily cron)."""
    now = time.time()
    with _db_lock, _conn() as c:
        states = c.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,)).rowcount
        codes = c.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,)).rowcount
        tokens = c.execute(
            "DELETE FROM oauth_access_tokens WHERE expires_at < ? AND revoked_at IS NOT NULL",
            (now,),
        ).rowcount
    return {"states": states, "codes": codes, "tokens": tokens}

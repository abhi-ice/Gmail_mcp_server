# Gmail MCP Server (v2 — multi-tenant)

A Gmail MCP server you can host once and share with your whole team. Each user authenticates with their own bearer token; their Gmail accounts and OAuth tokens are completely isolated from every other user's. No local Python install needed for end users — they just paste a config snippet into Claude Desktop.

Two modes:

| Mode | Transport | When |
|---|---|---|
| **stdio** | local | Solo developer running it on their own laptop. No bearer auth. |
| **http**  | remote streamable-HTTP | Hosted on a VPS. Each user has a bearer token. Multi-tenant. |

---

## What changed from v1

- **Multi-tenant**: every user is identified by an `Authorization: Bearer` token on every request. They only see their own connected Gmail accounts.
- **SQLite storage** at `$GMAIL_MCP_DATA_DIR/data.db`. Refresh tokens are encrypted with Fernet (key in env).
- **Remote OAuth flow**: `gmail_authenticate` now returns a URL to click. Google redirects to `/oauth/callback` on the server. No more spawning local HTTP servers on the user's laptop.
- **New tool**: `gmail_download_attachment` for fetching PDF/DOCX/image bytes by attachment ID.
- **Admin CLI**: `gmail-mcp-admin` for creating/revoking/rotating user tokens.
- **Deploy story**: Dockerfile, docker-compose, systemd unit, nginx config, full [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Tools

| Tool | Purpose |
|---|---|
| `gmail_list_accounts` | List the calling user's connected Gmail accounts |
| `gmail_authenticate` | Start OAuth flow for a new Gmail account (returns clickable URL) |
| `gmail_search_emails` | Gmail query syntax search |
| `gmail_read_email` | Body + headers + attachment metadata (with attachment IDs) |
| `gmail_download_attachment` | Download attachment bytes to disk |
| `gmail_draft_email` | Create draft (never sends) |
| `gmail_modify_email` | Trash, archive, label, star, mark read/unread |
| `gmail_list_labels` | List all labels with counts |
| `gmail_search_contacts` | Google People API search |

All tools take an `account` parameter — the alias of one of *your* connected Gmail accounts. Two users can both have `account="work"` pointing at completely different inboxes.

---

## Quick start — hosted deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full 10-step runbook (Hostinger VPS + nginx + Let's Encrypt + Google Cloud Console).

TL;DR:
1. DNS — point a subdomain at your VPS.
2. Google Cloud Console — add `https://your-domain/oauth/callback` as an authorized redirect URI; add team emails as Test users.
3. VPS — install, copy `.env.example` → `.env`, fill in values, start with systemd or docker-compose.
4. `gmail-mcp-admin user-create --email <user>@<domain>` for each team member.
5. Send each user the printed Claude Desktop config snippet.

---

## Quick start — local stdio mode

For solo development on your own machine:

```bash
git clone https://github.com/Abhinandan7619/GmailMcp.git
cd GmailMcp
python -m venv venv
source venv/bin/activate              # Windows: venv\Scripts\activate
pip install -e .

# Generate an encryption key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Create .env
cat > .env <<EOF
GMAIL_MCP_TRANSPORT=stdio
GMAIL_MCP_ENCRYPTION_KEY=<paste the key above>
GMAIL_MCP_OAUTH_CLIENT_ID=<from Google Cloud Console>
GMAIL_MCP_OAUTH_CLIENT_SECRET=<from Google Cloud Console>
GMAIL_MCP_OAUTH_REDIRECT_URI=http://localhost:8765/oauth/callback
EOF
```

Add to Claude Desktop config:
```json
{
  "mcpServers": {
    "gmail": {
      "command": "/full/path/to/venv/bin/gmail-mcp"
    }
  }
}
```

In stdio mode an implicit `local-dev` user is used — no bearer token needed.

---

## Architecture

```
Claude Desktop
    │
    │ stdio: spawns gmail-mcp directly
    │ http:  npx mcp-remote → HTTPS → nginx → uvicorn
    │
    ▼
gmail-mcp (FastMCP)
    │ ├── BearerAuthMiddleware (http only) — validates token, sets user context
    │ ├── /oauth/callback                   — Google → back to us
    │ └── /mcp                              — MCP streamable-HTTP endpoint
    │
    ▼
SQLite (data.db)
    users        (bearer_token_hash SHA256, revoked_at, is_admin)
    user_accounts (user_id, alias, email, token_encrypted Fernet)
    oauth_states  (state token, user_id, alias, 10-min TTL)
```

---

## Security model

| Surface | Mitigation |
|---|---|
| Bearer tokens at rest | Stored as SHA256 hash. Plaintext shown once at issuance. |
| Refresh tokens at rest | Fernet-encrypted (AES-128-CBC + HMAC) with key from env. |
| Path traversal in attachment downloads | `os.path.basename(filename)` strips path components. |
| Cross-user data access | Every tool reads `user_id` from contextvar — never from a tool parameter. Storage queries are always `WHERE user_id = ?`. |
| OAuth CSRF | Short-lived (10 min) single-use state tokens stored server-side. |
| Transport | nginx terminates TLS via Let's Encrypt. Bearer tokens never sent over plaintext. |

What this does **not** protect against:
- Compromise of the VPS itself (an attacker with root can read the encryption key from `.env` and decrypt all tokens). Use disk encryption + restricted SSH access.
- Malicious admin (anyone with `gmail-mcp-admin` access can issue tokens or revoke users).

---

## Updating

Server: `git pull && pip install -e . && sudo systemctl restart gmail-mcp`

User-side: no action.

---

## License

MIT

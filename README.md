# Gmail MCP Server (v3 — OAuth, zero per-user setup)

A self-hosted Gmail MCP server where **everyone in your organization pastes the same config snippet** into Claude Desktop. On first use, Claude Desktop opens a browser, the user signs in with Google, and that's it — no per-user tokens to issue, no admin involvement.

Per-user data isolation is enforced by the OAuth flow itself: each user's Google identity creates an isolated user record on the server. Their connected Gmail accounts are never visible to anyone else.

---

## How it works

```
Claude Desktop  ─[Authorization Code + PKCE]→  Gmail MCP  ─[Auth Code]→  Google
                                                    │
                                                    │  identity + Gmail scopes
                                                    ▼
                                              find_or_create user
                                              auto-connect 'primary' Gmail
                                                    │
                                                    ▼
Claude Desktop  ←[opaque access token]──── Gmail MCP
                       (cached locally, 30-day default TTL)
```

First-time user experience:

1. Paste config snippet → restart Claude Desktop
2. Ask Claude: "Search my Gmail for invoices"
3. Browser pops up automatically → **Sign in with Google** → grant Gmail access
4. Success page → go back to Claude
5. Gmail tools work

Adding more Gmail accounts later:
> "Authenticate my personal Gmail — alias 'personal'"
Claude returns a link. Click it, sign in with the other Google account, done.

---

## The universal config snippet

```json
{
  "mcpServers": {
    "gmail": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://gmail-mcp.your-domain.com/mcp"
      ]
    }
  }
}
```

Same snippet for every user. No token. No env vars.

---

## Tools

| Tool | Purpose |
|---|---|
| `gmail_list_accounts` | List your connected Gmail accounts |
| `gmail_authenticate` | Connect an additional Gmail account (returns click-link) |
| `gmail_search_emails` | Gmail query syntax search |
| `gmail_read_email` | Body + headers + attachment metadata (with attachment IDs) |
| `gmail_download_attachment` | Download attachment bytes (PDF, DOCX, etc.) to disk |
| `gmail_read_attachment` | Read attachment content inline as text (PDF/DOCX/XLSX/CSV…) |
| `gmail_export_email` | Export the whole email as `.eml` (RFC 822) — full headers, bodies and attachments |
| `gmail_draft_email` | Create draft, with optional file attachments (never sends) |
| `gmail_send_email` | Send immediately, with optional file attachments — cannot be recalled |
| `gmail_modify_email` | Trash, archive, label, star, mark read/unread |
| `gmail_list_labels` | List labels with counts |
| `gmail_search_contacts` | Google People API search |

All tools take an `account` parameter — the alias of one of *your* connected Gmails. Default after sign-in is `account="primary"`.

### Outbound mail

`gmail_draft_email` and `gmail_send_email` take the same arguments and build the
same MIME message — they differ only in which Gmail endpoint they hit.

- `attachments` — list of absolute paths to local files. Anything the server can
  read on its own filesystem. Combined size is capped at ~24 MB, because Gmail
  rejects messages over ~25 MB once base64-encoded.
- `html_body` — optional HTML alternative. When set, the message goes out as
  `multipart/alternative` and `body` becomes the plain-text fallback.

Note that in a hosted deployment `attachments` paths resolve on the *server*, not
on the machine running the MCP client.

---

## Access control

| Layer | Mechanism |
|---|---|
| Who can sign in (primary) | **Google Cloud Console → Test users**. Anyone not on this list is rejected by Google's consent screen. |
| Who can sign in (optional second layer) | Env vars `GMAIL_MCP_ALLOWED_DOMAINS` / `GMAIL_MCP_ALLOWED_EMAILS`. Useful if you ever move out of GCC Testing mode. |
| Per-user data isolation | Every storage query is `WHERE user_id = ?`. `user_id` is set from the OAuth access token in middleware, never from a tool parameter. |
| Revocation | `gmail-mcp-admin user-revoke --email sid@ice.com` blocks user + invalidates all their tokens. |

---

## Server modes

| Mode | Transport | When |
|---|---|---|
| **stdio** | local | Solo developer, no remote install. Single implicit local-dev user. |
| **http**  | streamable-HTTP | Hosted on a VPS. Full OAuth flow. Multi-tenant. |

---

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full 9-step runbook (Hostinger VPS + nginx + Let's Encrypt + Google Cloud Console).

TL;DR:
1. DNS → VPS
2. Google Cloud Console — add two redirect URIs + add team emails as Test users
3. VPS — pip install, fill `.env`, start with systemd
4. Send the universal snippet to your team

No `gmail-mcp-admin user-create` step. That's the point of v3.

---

## Ops commands

```bash
gmail-mcp-admin user-list                          # who's signed in
gmail-mcp-admin user-revoke --email <e>            # block a user
gmail-mcp-admin tokens-revoke-user --email <e>     # force re-sign-in (keeps user)
gmail-mcp-admin user-tokens --email <e>            # debug stale clients
gmail-mcp-admin config-snippet                     # print the universal snippet
gmail-mcp-admin cleanup                            # sweep expired artifacts (daily cron)
```

---

## Architecture

```
src/gmail_mcp/
  config.py          env loading, allowlist helpers
  storage.py         SQLite: users, user_accounts, oauth_clients,
                     oauth_codes, oauth_access_tokens, oauth_states
  crypto.py          Fernet + bearer token hashing
  context.py         contextvar for the current user_id
  auth.py            Google API service builders, refresh logic,
                     add-account OAuth flow
  oauth_server.py    OAuth 2.1 endpoints (authorize, token, register,
                     google-callback, metadata, revoke)
  middleware.py      Validate OAuth access tokens; 401 with WWW-Authenticate
  gmail_client.py    Gmail API wrapper (all per (user_id, alias))
  contacts_client.py People API wrapper
  server.py          FastMCP tools + transport selection + HTTP routes
  admin_cli.py       gmail-mcp-admin
```

---

## Security model

| Surface | Mitigation |
|---|---|
| Access tokens at rest | SHA256-hashed; we never store plaintext |
| Gmail refresh tokens at rest | Fernet (AES-128-CBC + HMAC) encrypted with env key |
| Authorization code reuse | Single-use, 10-minute TTL, deleted on consumption |
| Authorization code theft | PKCE (S256) required — possession of code alone is useless |
| Cross-user data access | `user_id` from validated token only; storage layer always scopes by user_id |
| Random sign-ups | GCC Test Users gate + optional env allowlist |
| MITM | nginx terminates TLS via Let's Encrypt |

Not protected against:
- VPS root compromise (attacker reads `.env`, decrypts tokens). Use disk encryption + restrict SSH.
- Malicious admin with CLI access can revoke + impersonate.

---

## Updating

`git pull && pip install -e . && systemctl restart gmail-mcp`. Users do nothing.

---

## License

MIT

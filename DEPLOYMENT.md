# Gmail MCP — Deployment Runbook (v3)

End-to-end setup for a single Hostinger VPS (or any Linux box). After deploy, **every user in your org pastes the same config snippet** — they sign in via Google in the browser, no admin token issuance needed.

Estimated time: 60–90 minutes (most of it DNS + Let's Encrypt).

---

## Prerequisites checklist

- [ ] A domain or subdomain — e.g. `gmail-mcp.ice.com`
- [ ] A Hostinger VPS (KVM 2 is plenty for ~20 users) running Ubuntu 22.04 / 24.04
- [ ] Root / sudo on the VPS
- [ ] A Google Cloud project with Gmail API + People API enabled
- [ ] An OAuth 2.0 Client ID (type: **Web application**)
- [ ] All ICE employees added as **Test users** in the OAuth consent screen (testing mode, up to 100 — fine for 20)

---

## Step 1 — DNS

Point your subdomain at the VPS public IP:

```
gmail-mcp.ice.com.  A  203.0.113.42      (TTL: 300)
```

Wait until `dig gmail-mcp.ice.com` returns the VPS IP (usually <5 min on Hostinger).

---

## Step 2 — Google Cloud Console

In your existing GCP project:

1. **APIs & Services → Library** — confirm **Gmail API** and **People API** are enabled.
2. **APIs & Services → Credentials** — open your OAuth 2.0 Client ID (type **Web application**):
   - **Authorized redirect URIs** — add BOTH of these (your domain, your callbacks):
     - `https://gmail-mcp.ice.com/oauth/google-callback` *(used by the user sign-in flow)*
     - `https://gmail-mcp.ice.com/oauth/callback` *(used when a user adds a second/third Gmail account)*
   - Save. Copy **Client ID** and **Client Secret**.
3. **APIs & Services → OAuth consent screen → Test users** — add every ICE employee email that will use this MCP. Testing-mode cap is 100, so you have plenty of headroom for 20-21.
   - This is your access allowlist. Anyone not on this list is rejected at Google's consent screen — they cannot sign in.

---

## Step 3 — Provision the VPS

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git nginx certbot python3-certbot-nginx ufw

sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable

sudo useradd -m -s /bin/bash gmail-mcp
sudo mkdir -p /opt/gmail-mcp/data
sudo chown -R gmail-mcp:gmail-mcp /opt/gmail-mcp
```

---

## Step 4 — Pull the code + install

```bash
sudo -u gmail-mcp -s
cd /opt/gmail-mcp
git clone https://github.com/Abhinandan7619/GmailMcp.git app
cd app
python3.12 -m venv /opt/gmail-mcp/venv
source /opt/gmail-mcp/venv/bin/activate
pip install -e .
```

---

## Step 5 — Create .env

```bash
ENC_KEY=$(/opt/gmail-mcp/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

cat > /opt/gmail-mcp/.env <<EOF
GMAIL_MCP_TRANSPORT=http
GMAIL_MCP_HTTP_HOST=127.0.0.1
GMAIL_MCP_HTTP_PORT=8765
GMAIL_MCP_PUBLIC_BASE_URL=https://gmail-mcp.ice.com
GMAIL_MCP_DATA_DIR=/opt/gmail-mcp/data
GMAIL_MCP_ENCRYPTION_KEY=$ENC_KEY
GMAIL_MCP_OAUTH_CLIENT_ID=<paste from GCC>
GMAIL_MCP_OAUTH_CLIENT_SECRET=<paste from GCC>
GMAIL_MCP_OAUTH_REDIRECT_URI=https://gmail-mcp.ice.com/oauth/callback
# Optional second-layer allowlist (GCC test users is the primary gate):
GMAIL_MCP_ALLOWED_DOMAINS=ice.com
EOF

chmod 600 /opt/gmail-mcp/.env
```

**Critical:** back up `.env` somewhere safe. Lose `GMAIL_MCP_ENCRYPTION_KEY` → every stored Gmail token is unrecoverable.

---

## Step 6 — systemd service

```bash
sudo cp /opt/gmail-mcp/app/deploy/gmail-mcp.service.example /etc/systemd/system/gmail-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now gmail-mcp
sudo systemctl status gmail-mcp
journalctl -u gmail-mcp -n 50 --no-pager

# Local sanity check
curl http://127.0.0.1:8765/health
# → {"status":"ok","transport":"http"}
```

---

## Step 7 — nginx + TLS

```bash
sudo cp /opt/gmail-mcp/app/deploy/nginx.conf.example /etc/nginx/sites-available/gmail-mcp
sudo nano /etc/nginx/sites-available/gmail-mcp     # replace gmail-mcp.example.com with your real domain
sudo ln -s /etc/nginx/sites-available/gmail-mcp /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d gmail-mcp.ice.com

curl https://gmail-mcp.ice.com/health
# → {"status":"ok","transport":"http"}

curl https://gmail-mcp.ice.com/.well-known/oauth-authorization-server | head
# Should return JSON with issuer/authorization_endpoint/token_endpoint
```

---

## Step 8 — Distribute the universal config snippet

That's it on the server side. To onboard users, send them this — **same snippet for everyone**:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://gmail-mcp.ice.com/mcp"
      ]
    }
  }
}
```

(Use `gmail-mcp-admin config-snippet` on the VPS to print this with your actual URL.)

### User-side steps (send this to each ICE employee):

1. Install Node.js if not already installed (for `npx mcp-remote`).
   - macOS: `brew install node`
   - Windows: download from nodejs.org
2. In Claude Desktop: **Settings → Developer → Edit Config**.
3. Paste the snippet above under `"mcpServers"`. Save. Quit + restart Claude Desktop.
4. Ask Claude: "List my Gmail accounts" — Claude Desktop will open a browser, prompt **Sign in with Google**, you grant Gmail access, you see a success page, you go back to Claude.
5. Done. Your primary Gmail is now connected as `account="primary"`.

To add another Gmail account (e.g., personal alongside work):
> "Authenticate my personal Gmail — alias 'personal', email 'me@gmail.com'"

Claude returns a click-link, you sign in with the *other* Google account, done.

---

## Step 9 — Ops commands

```bash
# Who's signed in?
sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin user-list

# Block someone (revokes user + all their access tokens):
sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin user-revoke --email sid@ice.com

# Force someone to re-sign-in (revokes only their access tokens):
sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin tokens-revoke-user --email sid@ice.com

# List a user's tokens (debugging stale clients):
sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin user-tokens --email sid@ice.com

# Reprint the universal config snippet:
sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin config-snippet

# Sweep expired states/codes/tokens (good as a daily cron):
sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin cleanup
```

Cron suggestion (root crontab):

```cron
0 4 * * * /usr/bin/sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin cleanup >/dev/null 2>&1
```

---

## Updating

```bash
sudo -u gmail-mcp -s
cd /opt/gmail-mcp/app
git pull
source /opt/gmail-mcp/venv/bin/activate
pip install -e .
exit
sudo systemctl restart gmail-mcp
```

User side: nothing. Their cached OAuth token keeps working until it expires.

---

## Backups

```cron
0 3 * * * tar czf /var/backups/gmail-mcp-$(date +\%Y\%m\%d).tar.gz /opt/gmail-mcp/data /opt/gmail-mcp/.env && find /var/backups -name 'gmail-mcp-*.tar.gz' -mtime +30 -delete
```

Ship the tarball off-box (S3, Hostinger object storage, etc.).

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Claude Desktop says "no MCP server" | mcp-remote not installed. Run `npx -y mcp-remote --version` manually to install it |
| Browser pops up but Google says "Access blocked: This app's request is invalid" | Redirect URI not registered in GCC. Re-check Step 2 — both `/oauth/google-callback` AND `/oauth/callback` must be added |
| Google says "gmail-mcp has not completed Google verification" | Normal in Testing mode — user clicks "Advanced → Go to gmail-mcp (unsafe)" |
| User signs in but sees "Sign-in not allowed for X" | Either GCC Testing mode hasn't added them as Test User, or your `GMAIL_MCP_ALLOWED_DOMAINS`/`_EMAILS` excludes them |
| OAuth callback shows "Google did not return refresh_token" | User has prior grant. Revoke at https://myaccount.google.com/permissions and retry |
| 502 Bad Gateway via nginx | `gmail-mcp.service` is down. `sudo systemctl status gmail-mcp` |
| Server logs "redirect_uri does not match" | Claude Desktop sent a localhost callback that wasn't seen in a prior `/oauth/register`. mcp-remote re-registers automatically; if it persists, restart Claude Desktop |

---

## Security notes

- GCC Test Users is the primary access gate. Production-mode OAuth would let any Google account sign in — DO NOT switch to Production unless you also set `GMAIL_MCP_ALLOWED_DOMAINS` or `GMAIL_MCP_ALLOWED_EMAILS`.
- All Gmail refresh tokens are Fernet-encrypted at rest with the env key.
- All access tokens are SHA256-hashed at rest.
- PKCE (S256) is required on the MCP OAuth flow — no token can be exchanged without the original code_verifier.
- Per-user data isolation: every storage query is `WHERE user_id = ?`; user_id always comes from the validated bearer token, never from a tool parameter.

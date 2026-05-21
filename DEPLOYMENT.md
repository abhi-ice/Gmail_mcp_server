# Gmail MCP — Deployment Runbook

End-to-end setup for the multi-tenant remote deployment on a Hostinger VPS (or any Linux box). Estimated time: 60–90 minutes including domain/DNS propagation.

---

## Prerequisites checklist

- [ ] A domain (or subdomain) you control — e.g. `gmail-mcp.ice.com`
- [ ] A Hostinger VPS (KVM 2 plan is plenty for ~20 users) running Ubuntu 22.04 or 24.04
- [ ] Root or sudo access on the VPS
- [ ] A Google Cloud project with Gmail API + People API enabled
- [ ] An OAuth 2.0 Client ID (type: **Web application**)
- [ ] All ICE employees who'll use this added as **Test users** in the OAuth consent screen (up to 100 in testing mode)

---

## Step 1 — DNS

Point your domain/subdomain at the VPS public IP via an `A` record.

```
gmail-mcp.ice.com.  A  203.0.113.42      (TTL: 300)
```

Wait until `dig gmail-mcp.ice.com` returns your VPS IP (usually <5 min on Hostinger).

---

## Step 2 — Google Cloud Console

In your existing GCP project:

1. **APIs & Services → Library** → ensure Gmail API + People API are enabled.
2. **APIs & Services → Credentials** → create or edit your OAuth 2.0 Client ID:
   - Application type: **Web application**
   - Authorized redirect URIs → add: `https://gmail-mcp.ice.com/oauth/callback`
   - Save. Copy the **Client ID** and **Client Secret**.
3. **APIs & Services → OAuth consent screen → Test users** → add every ICE employee email that will use this MCP. (Testing mode caps at 100 — fine for 20–21.)

---

## Step 3 — Provision the VPS

On the Hostinger VPS:

```bash
# Update + install base tools
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git nginx certbot python3-certbot-nginx ufw

# Firewall
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable

# Create a dedicated user
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

## Step 5 — Create the .env

```bash
# Generate a Fernet key
ENC_KEY=$(/opt/gmail-mcp/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

cat > /opt/gmail-mcp/.env <<EOF
GMAIL_MCP_TRANSPORT=http
GMAIL_MCP_HTTP_HOST=127.0.0.1
GMAIL_MCP_HTTP_PORT=8765
GMAIL_MCP_PUBLIC_BASE_URL=https://gmail-mcp.ice.com
GMAIL_MCP_DATA_DIR=/opt/gmail-mcp/data
GMAIL_MCP_ENCRYPTION_KEY=$ENC_KEY
GMAIL_MCP_OAUTH_CLIENT_ID=<paste from Google Cloud Console>
GMAIL_MCP_OAUTH_CLIENT_SECRET=<paste from Google Cloud Console>
GMAIL_MCP_OAUTH_REDIRECT_URI=https://gmail-mcp.ice.com/oauth/callback
EOF

chmod 600 /opt/gmail-mcp/.env
```

**Critical:** back up `.env` somewhere safe. Losing `GMAIL_MCP_ENCRYPTION_KEY` makes every stored Gmail token unrecoverable.

---

## Step 6 — systemd service

```bash
sudo cp /opt/gmail-mcp/app/deploy/gmail-mcp.service.example /etc/systemd/system/gmail-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now gmail-mcp
sudo systemctl status gmail-mcp           # should show "active (running)"
journalctl -u gmail-mcp -n 50 --no-pager  # check logs
```

Confirm the server is listening locally:

```bash
curl http://127.0.0.1:8765/health
# → {"status":"ok","transport":"http"}
```

---

## Step 7 — nginx + TLS

```bash
sudo cp /opt/gmail-mcp/app/deploy/nginx.conf.example /etc/nginx/sites-available/gmail-mcp
# Edit the file — replace gmail-mcp.example.com with your real domain
sudo nano /etc/nginx/sites-available/gmail-mcp

sudo ln -s /etc/nginx/sites-available/gmail-mcp /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Get a Let's Encrypt cert
sudo certbot --nginx -d gmail-mcp.ice.com
# Follow prompts. certbot edits the nginx config in place.

# Verify
curl https://gmail-mcp.ice.com/health
# → {"status":"ok","transport":"http"}
```

---

## Step 8 — Create users + bearer tokens

For every ICE employee who needs access:

```bash
sudo -u gmail-mcp /opt/gmail-mcp/venv/bin/gmail-mcp-admin user-create --email sid@ice.com
```

Output includes:
- A bearer token (**shown ONCE — save it**)
- A ready-to-paste Claude Desktop config snippet

Send the snippet (with the token) to the user via a secure channel — **not plain email**. 1Password, Bitwarden Send, Signal, etc.

To rotate a token: `gmail-mcp-admin user-rotate --email sid@ice.com`
To revoke access: `gmail-mcp-admin user-revoke --email sid@ice.com`

---

## Step 9 — User-side setup (what you send to Sid and others)

Each user needs:
1. Node.js installed (for `npx mcp-remote`). On macOS: `brew install node`. On Windows: download from nodejs.org.
2. Their Claude Desktop config updated. **Settings → Developer → Edit Config**, then paste the snippet you sent them under `mcpServers`. Restart Claude Desktop.

Example final config:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://gmail-mcp.ice.com/mcp",
        "--header",
        "Authorization: Bearer gmcp_<their-token>"
      ]
    }
  }
}
```

After restart, in Claude Desktop:
> "List my Gmail accounts" → returns "No accounts yet."
> "Authenticate my work email — alias 'work', email 'sid@ice.com'"
> → Claude calls `gmail_authenticate`, returns a URL. Sid clicks it → Google login → success page.
> Now: "Search my work inbox for recent invoices" → works.

---

## Step 10 — Verify everything

Run from your laptop:

```bash
# Should return service info
curl https://gmail-mcp.ice.com/

# Should be 401 (missing bearer)
curl -i https://gmail-mcp.ice.com/mcp

# With a real bearer:
curl -i https://gmail-mcp.ice.com/mcp -H "Authorization: Bearer gmcp_xxxxx"
# Expected: not 401. (May be a 4xx from MCP — that's fine, just confirming auth passes.)
```

---

## Updating the server later

```bash
sudo -u gmail-mcp -s
cd /opt/gmail-mcp/app
git pull
source /opt/gmail-mcp/venv/bin/activate
pip install -e .
exit
sudo systemctl restart gmail-mcp
journalctl -u gmail-mcp -n 20 --no-pager
```

User-side: no action required. Their Claude Desktop config keeps working.

---

## Backups

The only state worth backing up is `/opt/gmail-mcp/data/data.db` and `/opt/gmail-mcp/.env`. With both you can fully restore. Without `.env`'s encryption key, the DB is inert.

Cron job example (root):

```cron
0 3 * * * tar czf /var/backups/gmail-mcp-$(date +\%Y\%m\%d).tar.gz /opt/gmail-mcp/data /opt/gmail-mcp/.env && find /var/backups -name 'gmail-mcp-*.tar.gz' -mtime +30 -delete
```

Ship the tarball off-box to S3 / Hostinger object storage / wherever — don't keep backups only on the same VPS as the DB.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `gmail-mcp.service` won't start, exit code 1 | `.env` missing a required var. Run `journalctl -u gmail-mcp -n 50` |
| User gets "Missing or malformed Authorization header" | They pasted the snippet wrong; bearer token missing in the `--header` flag |
| OAuth callback shows "OAuth state token is invalid or has expired" | User waited >10 min, or the link was clicked twice. Just re-run `gmail_authenticate` |
| OAuth callback shows "Google did not return a refresh_token" | User already granted access to this client before. Revoke at https://myaccount.google.com/permissions and retry |
| User connects to MCP but can't see attachments correctly | Their Claude Desktop is on an old version. Update Claude Desktop |
| 502 Bad Gateway | gmail-mcp service is down. `sudo systemctl status gmail-mcp` |
| `mcp-remote` errors in Claude Desktop logs | Bad URL or expired token. Try `gmail-mcp-admin user-rotate` |

---

## Migrating Sid from the old single-user version

1. Issue Sid a bearer token: `gmail-mcp-admin user-create --email sid@ice.com`
2. Send him the new config snippet (replaces the old `command: gmail-mcp` entry in his Claude Desktop config).
3. He restarts Claude Desktop, runs `gmail_authenticate` for each Gmail account he had before. Old `~/.gmail-mcp/` on his laptop becomes unused and can be deleted.

There is no automated migration of his old tokens — the old format used the OAuth client's installed-app flow, the new one uses web-application flow. They're different OAuth credential types in Google. Re-authenticating is the clean path.

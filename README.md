# Gmail MCP Server

A production-grade, multi-account Gmail MCP server for Claude Desktop. It gives Claude access to multiple Gmail accounts simultaneously through a single local server instance — perfect for managing several company email accounts without switching contexts. The server communicates via stdio transport, runs entirely on your machine, and **never sends email**: it only creates drafts for your review.

---

## Prerequisites

- **Python 3.10+** installed on your system
- A **Google Cloud project** with the **Gmail API** and **People API** enabled
- An **OAuth 2.0 Client ID** (Desktop application type) downloaded from Google Cloud Console
- **Claude Desktop** (Mac or Windows)

---

## Installation

### 1. Clone / download the project

```bash
git clone <repo-url> gmail-mcp
cd gmail-mcp
```

### 2. Install the package

```bash
pip install -e .
```

This installs the `gmail-mcp` command and all dependencies (`mcp[cli]`, Google API client libraries, Pydantic).

### 3. Create the credentials directory

```bash
mkdir -p ~/.gmail-mcp
chmod 700 ~/.gmail-mcp
```

---

## Google Cloud OAuth Setup

### Step 1 — Create or open a Google Cloud project

1. Go to [https://console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown at the top → **New Project**
3. Name it (e.g., "Gmail MCP") and click **Create**

### Step 2 — Enable required APIs

1. In the left sidebar go to **APIs & Services → Library**
2. Search for **Gmail API** → click it → click **Enable**
3. Search for **People API** → click it → click **Enable**

### Step 3 — Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
2. Select **External** (or Internal if using Google Workspace)
3. Fill in the required fields:
   - App name: `Gmail MCP`
   - User support email: your email
   - Developer contact: your email
4. Click **Save and Continue**
5. On the **Scopes** page, click **Save and Continue** (scopes are requested at runtime)
6. On the **Test users** page, add each Gmail address you plan to authenticate
7. Click **Save and Continue**

### Step 4 — Create OAuth credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Name: `Gmail MCP Desktop Client`
5. Click **Create**
6. Click **Download JSON** on the confirmation dialog (or download icon next to the credential)
7. Save the file as:

```
~/.gmail-mcp/oauth-keys.json
```

```bash
chmod 600 ~/.gmail-mcp/oauth-keys.json
```

---

## Claude Desktop Configuration

Open your Claude Desktop config file:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add the following (replace the path to match your installation):

```json
{
  "mcpServers": {
    "gmail": {
      "command": "python",
      "args": ["-m", "gmail_mcp.server"],
      "cwd": "/path/to/gmail-mcp/src"
    }
  }
}
```

**Important:** Replace `/path/to/gmail-mcp/src` with the actual absolute path to the `src/` directory inside your cloned repo.

If you installed with `pip install -e .`, you can also use the installed script:

```json
{
  "mcpServers": {
    "gmail": {
      "command": "gmail-mcp"
    }
  }
}
```

Restart Claude Desktop after saving the config.

---

## Authenticating Accounts

Once Claude Desktop is running with the server configured, ask Claude to authenticate each account:

```
Authenticate my Gmail account with alias "company1", email "owner@company1.com",
description "Main construction company"
```

This will call `gmail_authenticate` which opens your default browser for Google OAuth sign-in. After signing in, the token is saved to `~/.gmail-mcp/accounts/company1/token.json`.

Repeat for each account:

```
gmail_authenticate(alias="company2", email="owner@company2.com", description="Site office")
gmail_authenticate(alias="company3", email="accounts@company3.com", description="Accounts payable")
```

**Alias rules:** lowercase letters, numbers, and hyphens only (e.g., `company1`, `site-office`, `accounts`).

Check your accounts any time:

```
gmail_list_accounts
```

---

## Available Tools

| Tool | Description |
|------|-------------|
| `gmail_list_accounts` | Show all connected accounts and their auth status |
| `gmail_authenticate` | Add or re-authenticate a Gmail account (opens browser) |
| `gmail_search_emails` | Search emails using Gmail query syntax |
| `gmail_read_email` | Read a full email including body and attachment info |
| `gmail_draft_email` | Create a draft email (never sends — drafts only) |
| `gmail_modify_email` | Trash, archive, star, mark read/unread, apply labels |
| `gmail_list_labels` | List all labels/folders with message counts |
| `gmail_search_contacts` | Search Google Contacts by name, email, or phone |

### Example queries

```
# Search for unread invoices in the accounts inbox
gmail_search_emails(account="accounts", query="subject:invoice is:unread")

# Read a specific email
gmail_read_email(account="company1", message_id="18d3f2a1b2c3d4e5")

# Draft a reply
gmail_draft_email(
    account="company1",
    to="contractor@example.com",
    subject="Re: Quote for Site 4",
    body="Thank you for the quote...",
    reply_to_message_id="18d3f2a1b2c3d4e5"
)

# Archive an email
gmail_modify_email(account="company1", message_id="18d3f2a1b2c3d4e5", action="archive")

# Search contacts
gmail_search_contacts(account="company1", query="Smith Concrete")
```

---

## Credential Storage

All credentials are stored locally on your machine under `~/.gmail-mcp/`:

```
~/.gmail-mcp/
├── oauth-keys.json           # Google Cloud OAuth client credentials (you provide this)
├── config.json               # Account aliases → email mapping (auto-managed)
└── accounts/
    ├── company1/
    │   └── token.json        # OAuth token (auto-managed, permissions 0o600)
    └── company2/
        └── token.json
```

Token files are created with `0o600` permissions (owner read/write only). The server never transmits credentials anywhere.

---

## Troubleshooting

### "OAuth keys file not found"

The file `~/.gmail-mcp/oauth-keys.json` is missing. Download your OAuth Client ID JSON from Google Cloud Console → **APIs & Services → Credentials** and save it there.

### "Token expired and no refresh token — re-authentication required"

Your OAuth token has been revoked or expired without a refresh token. Re-authenticate the affected account:

```
gmail_authenticate(alias="company1", email="owner@company1.com")
```

### "Rate limited by Gmail API — please wait a minute and try again"

You've hit Gmail API quota limits. Wait 60 seconds and retry. If this happens frequently, check your quota usage in Google Cloud Console → **APIs & Services → Gmail API → Quotas**.

### "Permission denied"

The OAuth token doesn't have the required scopes. This can happen if you added accounts before the full scope list was configured. Re-authenticate the account to get a fresh token with all required scopes.

### Server doesn't appear in Claude Desktop

1. Verify the `cwd` path in your Claude Desktop config is correct and the directory exists
2. Check that Python 3.10+ is available at the `command` path you specified
3. Restart Claude Desktop completely (quit from the tray/menu bar, not just close the window)
4. Check Claude Desktop logs for MCP connection errors

### Browser doesn't open during authentication

The OAuth flow uses `localhost` redirect. Make sure:
- You're running the server on the same machine as your browser
- No firewall blocks localhost connections
- If running in a headless environment, the OAuth flow cannot proceed — it requires a GUI browser

---

## Security Notes

- **No send capability**: The server uses `gmail.modify` and `gmail.compose` scopes, deliberately excluding `gmail.send`. Emails are only created as drafts.
- **Local only**: The server runs as a local process. No data leaves your machine except for direct Google API calls.
- **Token security**: Token files are stored with `0o600` permissions. Never commit `~/.gmail-mcp/` to version control.
- **Credential isolation**: Each account has its own token file. A revoked token for one account does not affect others.

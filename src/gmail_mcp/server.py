"""
Gmail MCP Server — multi-account Gmail access for Claude Desktop.
Transport: stdio (configured in Claude Desktop's mcpServers).
"""

import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool

from . import auth
from . import gmail_client
from . import contacts_client
from .models import (
    AuthenticateInput,
    DraftEmailInput,
    ListLabelsInput,
    ModifyEmailInput,
    ReadEmailInput,
    SearchContactsInput,
    SearchEmailsInput,
)
from .utils import handle_api_error, validate_account_alias

mcp = FastMCP("gmail_mcp")


# ---------------------------------------------------------------------------
# Tool 1: gmail_list_accounts
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_list_accounts",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def gmail_list_accounts() -> str:
    """Show all connected Gmail accounts and their authentication status."""
    try:
        config = auth.load_config()
        accounts = config.get("accounts", {})

        if not accounts:
            return (
                "No Gmail accounts configured yet.\n\n"
                "Use gmail_authenticate to add an account. Example:\n"
                "  gmail_authenticate(alias='company1', email='user@company1.com', description='Main office')"
            )

        lines = ["## Connected Gmail Accounts\n"]
        for alias, info in sorted(accounts.items()):
            email = info.get("email", "")
            description = info.get("description", "")
            status_info = auth.check_auth_status(alias)
            status_icon = "✓" if status_info["authenticated"] else "✗"
            status_text = status_info["status"]

            lines.append(f"**{alias}** [{status_icon}]")
            lines.append(f"  Email:       {email}")
            if description:
                lines.append(f"  Description: {description}")
            lines.append(f"  Status:      {status_text}")
            lines.append("")

        lines.append(f"Total accounts: {len(accounts)}")
        return "\n".join(lines)

    except Exception as e:
        print(f"[gmail-mcp] gmail_list_accounts error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 2: gmail_authenticate
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_authenticate",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def gmail_authenticate(alias: str, email: str, description: str = "") -> str:
    """
    Add or re-authenticate a Gmail account. Opens a browser for Google OAuth sign-in.

    Args:
        alias: Friendly name for the account (lowercase alphanumeric with hyphens, e.g. 'company1').
        email: Gmail address to authenticate (e.g. user@company1.com).
        description: Optional human-readable description (e.g. 'Main construction company email').
    """
    try:
        # Validate input via Pydantic
        validated = AuthenticateInput(
            alias=alias,
            email=email,
            description=description if description else None,
        )

        result = auth.authenticate_account(
            alias=validated.alias,
            email=validated.email,
            description=validated.description,
        )
        return (
            f"{result}\n\n"
            f"Account '{validated.alias}' is now ready to use. "
            f"Try gmail_search_emails(account='{validated.alias}', query='is:inbox') to get started."
        )

    except ValueError as e:
        return f"Invalid input: {e}"
    except FileNotFoundError as e:
        return str(e)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        print(f"[gmail-mcp] gmail_authenticate error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 3: gmail_search_emails
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_search_emails",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def gmail_search_emails(account: str, query: str, max_results: int = 10) -> str:
    """
    Search emails in a specific Gmail account using Gmail's full query syntax.

    Args:
        account: Account alias to search in.
        query: Gmail search query. Examples: 'from:client@example.com',
               'subject:invoice after:2024/01/01', 'is:unread in:inbox',
               'has:attachment label:important'.
        max_results: Maximum results to return (1-50, default 10).
    """
    try:
        validated = SearchEmailsInput(account=account, query=query, max_results=max_results)

        config = auth.load_config()
        err = validate_account_alias(validated.account, config)
        if err:
            return err

        messages = gmail_client.search_emails(
            alias=validated.account,
            query=validated.query,
            max_results=validated.max_results,
        )

        if not messages:
            return (
                f"No emails found for query: '{validated.query}' in account '{validated.account}'.\n\n"
                "Try a broader search. Examples:\n"
                "  • Remove date filters\n"
                "  • Use 'in:anywhere' to search all folders\n"
                "  • Simplify the query to a single keyword"
            )

        lines = [
            f"## Search Results — {validated.account}",
            f"Query: `{validated.query}`",
            f"Found: {len(messages)} result(s)\n",
        ]

        for i, msg in enumerate(messages, 1):
            lines.append(f"### {i}. {msg['subject']}")
            lines.append(f"**ID:**      `{msg['id']}`")
            lines.append(f"**From:**    {msg['from']}")
            lines.append(f"**To:**      {msg['to']}")
            if msg.get("cc"):
                lines.append(f"**Cc:**      {msg['cc']}")
            lines.append(f"**Date:**    {msg['date']}")
            if msg.get("labels"):
                lines.append(f"**Labels:**  {', '.join(msg['labels'])}")
            lines.append(f"**Preview:** {msg['snippet']}")
            lines.append("")

        return "\n".join(lines)

    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        print(f"[gmail-mcp] gmail_search_emails error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 4: gmail_read_email
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_read_email",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def gmail_read_email(account: str, message_id: str) -> str:
    """
    Read the full content of a specific email including body, headers, and attachment metadata.

    Args:
        account: Account alias.
        message_id: Email message ID from search results.
    """
    try:
        validated = ReadEmailInput(account=account, message_id=message_id)

        config = auth.load_config()
        err = validate_account_alias(validated.account, config)
        if err:
            return err

        email = gmail_client.read_email(
            alias=validated.account,
            message_id=validated.message_id,
        )

        lines = [
            f"## Email — {email['subject']}",
            "",
            f"**From:**    {email['from']}",
            f"**To:**      {email['to']}",
        ]
        if email.get("cc"):
            lines.append(f"**Cc:**      {email['cc']}")
        lines.append(f"**Date:**    {email['date']}")
        lines.append(f"**Message ID:** `{email['id']}`")
        if email.get("labels"):
            lines.append(f"**Labels:**  {', '.join(email['labels'])}")

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(email["body"])

        if email.get("attachments"):
            lines.append("")
            lines.append("---")
            lines.append(f"**Attachments ({len(email['attachments'])}):**")
            for att in email["attachments"]:
                lines.append(
                    f"  • {att['filename']} ({att['mimeType']}, {att['size']})"
                )

        return "\n".join(lines)

    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        print(f"[gmail-mcp] gmail_read_email error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 5: gmail_draft_email
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_draft_email",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def gmail_draft_email(
    account: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    reply_to_message_id: str = "",
) -> str:
    """
    Create an email draft in a specific account. The draft appears in Gmail's Drafts folder
    for manual review and sending. This tool never sends email — only creates drafts.

    Args:
        account: Account alias to create draft in.
        to: Recipient email address(es), comma-separated.
        subject: Email subject line.
        body: Email body text (plain text).
        cc: CC recipients, comma-separated (optional).
        bcc: BCC recipients, comma-separated (optional).
        reply_to_message_id: Original message ID if this is a reply (optional). Sets In-Reply-To,
                             References, and threadId for proper Gmail threading.
    """
    try:
        validated = DraftEmailInput(
            account=account,
            to=to,
            subject=subject,
            body=body,
            cc=cc if cc else None,
            bcc=bcc if bcc else None,
            reply_to_message_id=reply_to_message_id if reply_to_message_id else None,
        )

        config = auth.load_config()
        err = validate_account_alias(validated.account, config)
        if err:
            return err

        result = gmail_client.create_draft(
            alias=validated.account,
            to=validated.to,
            subject=validated.subject,
            body=validated.body,
            cc=validated.cc,
            bcc=validated.bcc,
            reply_to_message_id=validated.reply_to_message_id,
        )

        account_info = config["accounts"].get(validated.account, {})
        sender_email = account_info.get("email", validated.account)

        lines = [
            "## Draft Created Successfully",
            "",
            f"**Draft ID:**   `{result['draft_id']}`",
            f"**From:**       {sender_email}",
            f"**To:**         {validated.to}",
        ]
        if validated.cc:
            lines.append(f"**Cc:**         {validated.cc}")
        if validated.bcc:
            lines.append(f"**Bcc:**        {validated.bcc}")
        lines.append(f"**Subject:**    {validated.subject}")
        if result.get("thread_id"):
            lines.append(f"**Thread ID:**  `{result['thread_id']}`")
        lines.append("")
        lines.append(
            "The draft has been saved to the Drafts folder in Gmail. "
            "Open Gmail to review and send it."
        )

        return "\n".join(lines)

    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        print(f"[gmail-mcp] gmail_draft_email error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 6: gmail_modify_email
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_modify_email",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def gmail_modify_email(
    account: str,
    message_id: str,
    action: str,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
) -> str:
    """
    Modify an email — apply/remove labels, trash, archive, mark read/unread, star/unstar.

    Args:
        account: Account alias.
        message_id: Email message ID.
        action: One of 'trash', 'untrash', 'archive', 'move_to_inbox',
                'mark_read', 'mark_unread', 'star', 'unstar'.
        add_labels: Additional label names or IDs to add (optional).
        remove_labels: Label names or IDs to remove (optional).
    """
    try:
        validated = ModifyEmailInput(
            account=account,
            message_id=message_id,
            action=action,
            add_labels=add_labels,
            remove_labels=remove_labels,
        )

        config = auth.load_config()
        err = validate_account_alias(validated.account, config)
        if err:
            return err

        result = gmail_client.modify_email(
            alias=validated.account,
            message_id=validated.message_id,
            action=validated.action.value,
            add_labels=validated.add_labels,
            remove_labels=validated.remove_labels,
        )

        action_descriptions = {
            "trash":         "Moved to Trash",
            "untrash":       "Removed from Trash",
            "archive":       "Archived (removed from Inbox)",
            "move_to_inbox": "Moved to Inbox",
            "mark_read":     "Marked as read",
            "mark_unread":   "Marked as unread",
            "star":          "Starred",
            "unstar":        "Unstarred",
        }
        action_desc = action_descriptions.get(validated.action.value, validated.action.value)

        lines = [
            f"## Email Modified — {action_desc}",
            "",
            f"**Message ID:** `{result['message_id']}`",
            f"**Action:**     {action_desc}",
            f"**Current Labels:** {', '.join(result['labels']) if result['labels'] else '(none)'}",
        ]

        return "\n".join(lines)

    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        print(f"[gmail-mcp] gmail_modify_email error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 7: gmail_list_labels
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_list_labels",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def gmail_list_labels(account: str) -> str:
    """
    List all labels/folders for a specific Gmail account.

    Args:
        account: Account alias.
    """
    try:
        validated = ListLabelsInput(account=account)

        config = auth.load_config()
        err = validate_account_alias(validated.account, config)
        if err:
            return err

        labels = gmail_client.list_labels(alias=validated.account)

        system_labels = [l for l in labels if l["type"] == "system"]
        user_labels = [l for l in labels if l["type"] != "system"]

        lines = [f"## Labels — {validated.account}\n"]

        if system_labels:
            lines.append("### System Labels")
            for lbl in system_labels:
                unread = lbl["unread_messages"]
                total = lbl["total_messages"]
                count_str = f" ({unread} unread / {total} total)" if total else ""
                lines.append(f"  **{lbl['name']}**{count_str}")
                lines.append(f"    ID: `{lbl['id']}`")
            lines.append("")

        if user_labels:
            lines.append("### Custom Labels")
            for lbl in user_labels:
                unread = lbl["unread_messages"]
                total = lbl["total_messages"]
                count_str = f" ({unread} unread / {total} total)" if total else ""
                lines.append(f"  **{lbl['name']}**{count_str}")
                lines.append(f"    ID: `{lbl['id']}`")
            lines.append("")

        lines.append(
            f"Total: {len(system_labels)} system label(s), {len(user_labels)} custom label(s)"
        )

        return "\n".join(lines)

    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        print(f"[gmail-mcp] gmail_list_labels error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 8: gmail_search_contacts
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_search_contacts",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def gmail_search_contacts(account: str, query: str) -> str:
    """
    Search Gmail contacts in a specific account using Google People API.

    Args:
        account: Account alias.
        query: Name, email, or phone number to search for.
    """
    try:
        validated = SearchContactsInput(account=account, query=query)

        config = auth.load_config()
        err = validate_account_alias(validated.account, config)
        if err:
            return err

        contacts = contacts_client.search_contacts(
            alias=validated.account,
            query=validated.query,
        )

        if not contacts:
            return (
                f"No contacts found for query '{validated.query}' in account '{validated.account}'.\n"
                "Try searching by first name, last name, or email domain."
            )

        lines = [
            f"## Contacts — {validated.account}",
            f"Query: '{validated.query}'",
            f"Found: {len(contacts)} contact(s)\n",
        ]

        for i, contact in enumerate(contacts, 1):
            lines.append(f"### {i}. {contact['name'] or '(no name)'}")
            if contact["emails"]:
                lines.append(f"  **Email(s):** {', '.join(contact['emails'])}")
            if contact["phones"]:
                phone_strs = [
                    f"{p['number']} ({p['type']})" if p["type"] else p["number"]
                    for p in contact["phones"]
                ]
                lines.append(f"  **Phone(s):** {', '.join(phone_strs)}")
            if contact["organization"]:
                org_str = contact["organization"]
                if contact["title"]:
                    org_str += f", {contact['title']}"
                lines.append(f"  **Organization:** {org_str}")
            lines.append("")

        return "\n".join(lines)

    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        print(f"[gmail-mcp] gmail_search_contacts error: {e}", file=sys.stderr)
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the Gmail MCP server via stdio transport."""
    print("[gmail-mcp] Starting Gmail MCP server (stdio transport)...", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""
Gmail MCP Server — multi-tenant (stdio OR remote streamable-HTTP).

Transports:
- stdio: local single-user dev mode (no bearer auth; implicit local-dev user)
- http:  remote multi-tenant mode (bearer auth required on every request)

Tools call require_user_id() to get the calling user — they never trust a
parameter for identity. The Gmail `account` parameter is just an alias
scoped to the calling user.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Optional

from urllib.parse import quote, urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Mount, Route

from . import auth, context, gmail_client, contacts_client, oauth_server, storage
from .config import (
    DOWNLOADS_DIR,
    HTTP_HOST,
    HTTP_PORT,
    READ_INLINE_MAX_CHARS,
    TRANSPORT,
    ensure_downloads_dir,
    get_local_download_dir,
    get_public_base_url,
    log,
)
from .middleware import OAuthAccessTokenMiddleware
from .models import (
    AuthenticateInput,
    DownloadAttachmentInput,
    DraftEmailInput,
    ExportEmailInput,
    ListLabelsInput,
    ModifyEmailInput,
    ReadAttachmentInput,
    ReadEmailInput,
    SearchContactsInput,
    SearchEmailsInput,
)
from .utils import extract_attachment_text, format_size, handle_api_error, validate_account_alias


def _build_transport_security() -> TransportSecuritySettings:
    """
    FastMCP's DNS-rebinding protection rejects Host headers not on this list.
    Default only allows localhost; we add our public hostname so production
    requests aren't blocked with 421 Misdirected Request.
    """
    allowed = ["127.0.0.1", "localhost", f"127.0.0.1:{HTTP_PORT}", f"localhost:{HTTP_PORT}"]
    if TRANSPORT == "http":
        try:
            host = urlparse(get_public_base_url()).hostname
            if host:
                allowed.append(host)
        except Exception:
            pass
    return TransportSecuritySettings(allowed_hosts=allowed)


mcp = FastMCP("gmail_mcp", transport_security=_build_transport_security())


def _get_user_id() -> str:
    """Resolve the calling user. In stdio mode, falls back to the local-dev user."""
    uid = context.get_current_user_id()
    if uid is not None:
        return uid
    if TRANSPORT == "stdio":
        return storage.ensure_local_dev_user()
    raise RuntimeError(
        "No authenticated user. This should have been rejected by the auth middleware."
    )


def _user_accounts_dict(user_id: str) -> dict:
    """Shape stored accounts to look like the old config dict for validate_account_alias()."""
    accounts = storage.list_accounts(user_id)
    return {"accounts": {a["alias"]: a for a in accounts}}


# ---------------------------------------------------------------------------
# Tool 1: gmail_list_accounts
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_list_accounts",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def gmail_list_accounts() -> str:
    """Show all Gmail accounts the calling user has connected."""
    try:
        user_id = _get_user_id()
        accounts = storage.list_accounts(user_id)

        if not accounts:
            return (
                "No Gmail accounts connected yet.\n\n"
                "Use gmail_authenticate to connect one. Example:\n"
                "  gmail_authenticate(alias='work', email='you@example.com', description='Main work email')"
            )

        lines = ["## Your Connected Gmail Accounts\n"]
        for a in accounts:
            status_info = auth.check_auth_status(user_id, a["alias"])
            status_icon = "✓" if status_info["authenticated"] else "✗"
            lines.append(f"**{a['alias']}** [{status_icon}]")
            lines.append(f"  Email:       {a['email']}")
            if a.get("description"):
                lines.append(f"  Description: {a['description']}")
            lines.append(f"  Status:      {status_info['status']}")
            lines.append("")

        lines.append(f"Total accounts: {len(accounts)}")
        return "\n".join(lines)
    except Exception as e:
        log(f"gmail_list_accounts error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 2: gmail_authenticate
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_authenticate",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_authenticate(alias: str, email: str, description: str = "") -> str:
    """
    Start the OAuth flow to connect a Gmail account to your user.
    Returns a URL — open it in your browser to grant access.

    Args:
        alias: Friendly name (lowercase alphanumeric + hyphens, e.g. 'work').
        email: Gmail address you'll authenticate.
        description: Optional human-readable description.
    """
    try:
        validated = AuthenticateInput(
            alias=alias, email=email, description=description if description else None
        )
        user_id = _get_user_id()

        consent_url = auth.start_oauth_flow(
            user_id=user_id,
            alias=validated.alias,
            email=validated.email,
            description=validated.description,
        )

        return (
            f"## Authenticate account '{validated.alias}'\n\n"
            f"**Open this URL in your browser to grant access:**\n\n"
            f"{consent_url}\n\n"
            f"After granting access you'll see a success page. Then come back here and try "
            f"`gmail_list_accounts` or any other Gmail tool with `account=\"{validated.alias}\"`.\n\n"
            f"The link expires in 10 minutes."
        )
    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log(f"gmail_authenticate error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 3: gmail_search_emails
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_search_emails",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_search_emails(account: str, query: str, max_results: int = 10) -> str:
    """
    Search emails using Gmail's full query syntax.

    Args:
        account: Account alias (your own).
        query: Gmail query (e.g. 'has:attachment from:client@example.com').
        max_results: 1–50, default 10.
    """
    try:
        validated = SearchEmailsInput(account=account, query=query, max_results=max_results)
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        messages = gmail_client.search_emails(
            user_id=user_id,
            alias=validated.account,
            query=validated.query,
            max_results=validated.max_results,
        )

        if not messages:
            return (
                f"No emails found for query: '{validated.query}' in account '{validated.account}'.\n\n"
                "Try a broader search."
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
        log(f"gmail_search_emails error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 4: gmail_read_email
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_read_email",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_read_email(account: str, message_id: str) -> str:
    """Read body, headers, and attachment metadata for an email."""
    try:
        validated = ReadEmailInput(account=account, message_id=message_id)
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        email = gmail_client.read_email(user_id, validated.account, validated.message_id)

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
                lines.append(f"  • {att['filename']} ({att['mimeType']}, {att['size']})")
                if att.get("attachmentId"):
                    lines.append(f"    Attachment ID: `{att['attachmentId']}`")
            lines.append("")
            lines.append(
                "_To download an attachment, call `gmail_download_attachment` with the Message ID, Attachment ID, and a filename._"
            )

        return "\n".join(lines)
    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log(f"gmail_read_email error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 5: gmail_draft_email
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_draft_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
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
    """Create an email draft. Never sends — appears in Gmail Drafts for manual review."""
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
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        result = gmail_client.create_draft(
            user_id=user_id,
            alias=validated.account,
            to=validated.to,
            subject=validated.subject,
            body=validated.body,
            cc=validated.cc,
            bcc=validated.bcc,
            reply_to_message_id=validated.reply_to_message_id,
        )

        account_info = storage.get_account(user_id, validated.account)
        sender_email = account_info["email"] if account_info else validated.account

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
        lines.append("The draft has been saved. Open Gmail to review and send.")
        return "\n".join(lines)
    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log(f"gmail_draft_email error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 6: gmail_modify_email
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_modify_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_modify_email(
    account: str,
    message_id: str,
    action: str,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
) -> str:
    """Apply/remove labels, trash, archive, mark read/unread, star/unstar."""
    try:
        validated = ModifyEmailInput(
            account=account,
            message_id=message_id,
            action=action,
            add_labels=add_labels,
            remove_labels=remove_labels,
        )
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        result = gmail_client.modify_email(
            user_id=user_id,
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
        log(f"gmail_modify_email error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 7: gmail_list_labels
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_list_labels",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_list_labels(account: str) -> str:
    """List all labels/folders for an account."""
    try:
        validated = ListLabelsInput(account=account)
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        labels = gmail_client.list_labels(user_id, validated.account)
        system_labels = [l for l in labels if l["type"] == "system"]
        user_labels = [l for l in labels if l["type"] != "system"]

        lines = [f"## Labels — {validated.account}\n"]
        if system_labels:
            lines.append("### System Labels")
            for lbl in system_labels:
                unread, total = lbl["unread_messages"], lbl["total_messages"]
                count_str = f" ({unread} unread / {total} total)" if total else ""
                lines.append(f"  **{lbl['name']}**{count_str}")
                lines.append(f"    ID: `{lbl['id']}`")
            lines.append("")
        if user_labels:
            lines.append("### Custom Labels")
            for lbl in user_labels:
                unread, total = lbl["unread_messages"], lbl["total_messages"]
                count_str = f" ({unread} unread / {total} total)" if total else ""
                lines.append(f"  **{lbl['name']}**{count_str}")
                lines.append(f"    ID: `{lbl['id']}`")
            lines.append("")
        lines.append(f"Total: {len(system_labels)} system, {len(user_labels)} custom")
        return "\n".join(lines)
    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log(f"gmail_list_labels error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 8: gmail_search_contacts
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_search_contacts",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_search_contacts(account: str, query: str) -> str:
    """Search contacts via Google People API."""
    try:
        validated = SearchContactsInput(account=account, query=query)
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        contacts = contacts_client.search_contacts(user_id, validated.account, validated.query)
        if not contacts:
            return f"No contacts found for query '{validated.query}'."

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
        log(f"gmail_search_contacts error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Attachment delivery helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    safe = os.path.basename((name or "").strip())
    if not safe or safe in (".", ".."):
        return "attachment.bin"
    return safe


def _dedupe_path(path: Path) -> Path:
    """If path exists, append (1), (2), ... before the extension."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _save_to_local_downloads(binary: bytes, filename: str) -> str:
    """stdio mode only: write the file into the user's local Downloads folder."""
    d = get_local_download_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = _dedupe_path(d / _safe_filename(filename))
    target.write_bytes(binary)
    return str(target)


def _persist_for_download(user_id: str, binary: bytes, filename: str, mime: str) -> dict:
    """
    http mode: stash the bytes on the server under a random dir, register a
    short-lived token, and return a signed /files/<token>/<name> URL.
    """
    ensure_downloads_dir()
    safe = _safe_filename(filename)
    subdir = DOWNLOADS_DIR / secrets.token_hex(16)
    subdir.mkdir(parents=True, exist_ok=True)
    fpath = subdir / safe
    fpath.write_bytes(binary)
    token = storage.create_download_token(
        user_id=user_id, path=str(fpath), filename=safe, mime=mime, size_bytes=len(binary)
    )
    url = f"{get_public_base_url()}/files/{token}/{quote(safe)}"
    return {"url": url, "filename": safe}


# ---------------------------------------------------------------------------
# Tool 9: gmail_download_attachment
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_download_attachment",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_download_attachment(
    account: str,
    message_id: str,
    attachment_id: str,
    filename: str = "",
) -> str:
    """
    Download an email attachment of ANY type and size — PDF, DOCX, XLSX, images,
    ZIPs, anything. No size restriction beyond the server's safety ceiling.

    Returns a direct, time-limited download link (valid ~1 hour). Open the link
    in a browser to save the file, or fetch it programmatically. The real
    filename is auto-detected; pass `filename` only to override it.

    (When the server runs locally in stdio mode, the file is written straight
    into your Downloads folder instead and the local path is returned.)

    Args:
        account: Account alias.
        message_id: Email message ID.
        attachment_id: Attachment ID from gmail_read_email output.
        filename: Optional override for the saved filename.
    """
    try:
        validated = DownloadAttachmentInput(
            account=account,
            message_id=message_id,
            attachment_id=attachment_id,
            filename=filename or None,
        )
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        info = gmail_client.resolve_attachment(
            user_id=user_id,
            alias=validated.account,
            message_id=validated.message_id,
            attachment_id=validated.attachment_id,
        )
        binary = info["binary"]
        mime = info["mime_type"]
        fname = validated.filename or info["filename"]

        if TRANSPORT == "stdio":
            path = _save_to_local_downloads(binary, fname)
            lines = [
                "## Attachment Downloaded",
                "",
                f"**Saved to:** `{path}`",
                f"**Filename:** {_safe_filename(fname)}",
                f"**Type:**     {mime}",
                f"**Size:**     {format_size(info['size_bytes'])}",
            ]
            return "\n".join(lines)

        result = _persist_for_download(user_id, binary, fname, mime)
        lines = [
            "## Attachment Ready",
            "",
            f"**Filename:**     {result['filename']}",
            f"**Type:**         {mime}",
            f"**Size:**         {format_size(info['size_bytes'])}",
            "",
            f"**Download link:** {result['url']}",
            "",
            "_Open the link in a browser to save the file (valid ~1 hour). "
            "If you're an automated client, fetch that URL directly to retrieve the bytes._",
        ]
        return "\n".join(lines)
    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log(f"gmail_download_attachment error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 10: gmail_export_email
# ---------------------------------------------------------------------------

def _ensure_eml_ext(name: str) -> str:
    safe = _safe_filename(name)
    return safe if safe.lower().endswith(".eml") else f"{safe}.eml"


@mcp.tool(
    name="gmail_export_email",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_export_email(
    account: str,
    message_id: str,
    filename: str = "",
) -> str:
    """
    Export a whole email as a .eml file (RFC 822) — the complete original
    message: every header, the plain-text AND HTML bodies, inline images and
    all attachments, byte-for-byte as it was received.

    Use this to archive a message, hand it to another mail client, keep it as a
    record, or as the source for converting an email to PDF. Unlike
    gmail_read_email (which returns a readable summary), this preserves the
    message exactly, so nothing is lost.

    Returns a direct, time-limited download link (valid ~1 hour). Fetch that URL
    to retrieve the bytes.

    (When the server runs locally in stdio mode, the file is written straight
    into your Downloads folder instead and the local path is returned.)

    Args:
        account: Account alias.
        message_id: Email message ID (from gmail_search_emails or gmail_read_email).
        filename: Optional override. A .eml extension is added if missing.
    """
    try:
        validated = ExportEmailInput(
            account=account,
            message_id=message_id,
            filename=filename or None,
        )
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        info = gmail_client.export_email_raw(
            user_id=user_id,
            alias=validated.account,
            message_id=validated.message_id,
        )
        binary = info["binary"]
        fname = _ensure_eml_ext(validated.filename or info["filename"])

        if TRANSPORT == "stdio":
            path = _save_to_local_downloads(binary, fname)
            lines = [
                "## Email Exported (.eml)",
                "",
                f"**Saved to:** `{path}`",
                f"**Subject:**  {info['subject']}",
                f"**Date:**     {info['date'] or '(unknown)'}",
                f"**Size:**     {format_size(info['size_bytes'])}",
            ]
            return "\n".join(lines)

        result = _persist_for_download(user_id, binary, fname, info["mime_type"])
        lines = [
            "## Email Exported (.eml)",
            "",
            f"**Filename:** {result['filename']}",
            f"**Subject:**  {info['subject']}",
            f"**Date:**     {info['date'] or '(unknown)'}",
            f"**Size:**     {format_size(info['size_bytes'])}",
            "",
            f"**Download link:** {result['url']}",
            "",
            "_Open the link in a browser to save the file (valid ~1 hour). "
            "If you're an automated client, fetch that URL directly to retrieve the bytes. "
            "The .eml opens in Outlook/Thunderbird/Apple Mail, and can be converted to PDF._",
        ]
        return "\n".join(lines)
    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log(f"gmail_export_email error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# Tool 11: gmail_read_attachment
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gmail_read_attachment",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def gmail_read_attachment(
    account: str,
    message_id: str,
    attachment_id: str,
) -> str:
    """
    Read the FULL CONTENT of an email attachment inline.

    - PDF / DOCX / XLSX / TXT / CSV / JSON / XML / HTML  → returns the extracted
      TEXT so you can read or summarize it directly. No file-size limit (text of
      very large docs is truncated for display; the full file stays available
      via gmail_download_attachment).
    - Images, ZIPs, and other binary formats → can't be turned into text, so
      this returns a direct download link instead (see gmail_download_attachment).

    Args:
        account: Account alias.
        message_id: Email message ID.
        attachment_id: Attachment ID from gmail_read_email output.
    """
    try:
        validated = ReadAttachmentInput(
            account=account,
            message_id=message_id,
            attachment_id=attachment_id,
        )
        user_id = _get_user_id()
        err = validate_account_alias(validated.account, _user_accounts_dict(user_id))
        if err:
            return err

        info = gmail_client.resolve_attachment(
            user_id=user_id,
            alias=validated.account,
            message_id=validated.message_id,
            attachment_id=validated.attachment_id,
        )

        binary = info["binary"]
        mime = info["mime_type"]
        filename = info["filename"]

        text_content, fmt_label = extract_attachment_text(binary, mime, filename)

        lines = [
            f"## Attachment: {filename}",
            "",
            f"**MIME type:** `{mime}`",
            f"**Size:**      {format_size(info['size_bytes'])}",
        ]

        if text_content is not None:
            truncated = False
            if READ_INLINE_MAX_CHARS and len(text_content) > READ_INLINE_MAX_CHARS:
                text_content = text_content[:READ_INLINE_MAX_CHARS]
                truncated = True
            lines.append(f"**Extraction:** {fmt_label}")
            lines.append("")
            lines.append("---")
            lines.append("")
            lines.append(text_content)
            if truncated:
                lines.append("")
                lines.append(
                    f"_… text truncated at {READ_INLINE_MAX_CHARS:,} characters. "
                    f"Use gmail_download_attachment to get the complete file._"
                )
            return "\n".join(lines)

        # Non-text (image / binary): give a download link rather than useless base64.
        lines.append("**Extraction:** not a text format — provided as a download instead.")
        lines.append("")
        if TRANSPORT == "stdio":
            path = _save_to_local_downloads(binary, filename)
            lines.append(f"**Saved to:** `{path}`")
        else:
            result = _persist_for_download(user_id, binary, filename, mime)
            lines.append(f"**Download link:** {result['url']}")
            lines.append("")
            lines.append("_Open the link in a browser to save it (valid ~1 hour)._")
        return "\n".join(lines)

    except ValueError as e:
        return f"Invalid input: {e}"
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        log(f"gmail_read_attachment error: {e}")
        return handle_api_error(e)


# ---------------------------------------------------------------------------
# HTTP routes (used in http transport mode)
# ---------------------------------------------------------------------------

async def oauth_callback(request: Request):
    """Google redirects here after the user grants consent."""
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    error = request.query_params.get("error", "")

    if error:
        return HTMLResponse(_html_status(False, f"Google returned an error: {error}"), status_code=400)
    if not state or not code:
        return HTMLResponse(_html_status(False, "Missing state or code parameter."), status_code=400)

    try:
        ctx = auth.complete_oauth_flow(state, code)
    except Exception as e:
        log(f"OAuth callback error: {e}")
        return HTMLResponse(_html_status(False, f"OAuth callback failed: {e}"), status_code=400)

    msg = (
        f"Account <strong>{ctx['alias']}</strong> ({ctx['email']}) is now connected. "
        f"You can close this tab and go back to Claude Desktop."
    )
    return HTMLResponse(_html_status(True, msg))


async def serve_file(request: Request):
    """
    Serve a previously-downloaded attachment by its signed token.
    Capability URL: the token is the secret, so this route is exempt from
    bearer auth (a browser click has no Authorization header). Tokens are
    unguessable and expire (~1 hour).
    """
    token = request.path_params.get("token", "")
    rec = storage.get_download_token(token)
    if not rec:
        return PlainTextResponse("Not found, or this download link has expired.", status_code=404)
    path = rec["path"]
    if not os.path.isfile(path):
        return PlainTextResponse("File is no longer available on the server.", status_code=404)
    return FileResponse(
        path,
        filename=rec["filename"],
        media_type=rec["mime"] or "application/octet-stream",
    )


async def health(_request: Request):
    return JSONResponse({"status": "ok", "transport": TRANSPORT})


async def root(_request: Request):
    return JSONResponse(
        {
            "service": "gmail-mcp",
            "version": "2.0.0",
            "mcp_endpoint": "/mcp",
            "docs": "See DEPLOYMENT.md for setup.",
        }
    )


def _html_status(success: bool, message: str) -> str:
    color = "#1a7f37" if success else "#c33"
    icon = "✓" if success else "✗"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gmail MCP</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#f5f1ea; color:#1a2845;
          display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:white; padding:48px; border-radius:12px; box-shadow:0 4px 24px rgba(0,0,0,0.08);
           max-width:520px; text-align:center; }}
  .icon {{ font-size:64px; color:{color}; line-height:1; margin-bottom:16px; }}
  h1 {{ font-size:20px; margin:0 0 12px; }}
  p {{ color:#555; line-height:1.6; margin:0; }}
</style></head>
<body><div class="card"><div class="icon">{icon}</div>
<h1>{"Success" if success else "Authentication failed"}</h1>
<p>{message}</p></div></body></html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_stdio() -> None:
    storage.init_db()
    storage.ensure_local_dev_user()
    log("Starting Gmail MCP server (stdio transport, local-dev user)")
    mcp.run(transport="stdio")


def _run_http() -> None:
    import contextlib
    import uvicorn

    storage.init_db()
    log(f"Starting Gmail MCP server (http transport) on {HTTP_HOST}:{HTTP_PORT}")
    log(f"Public base URL: {get_public_base_url()}")

    mcp_app = mcp.streamable_http_app()

    # CRITICAL: when mounting FastMCP's app inside a parent Starlette app, the
    # parent owns the lifespan, so we must propagate the child's lifespan to
    # start MCP's session_manager task group. Without this, POST /mcp returns
    # 500 "Task group is not initialized. Make sure to use run()."
    @contextlib.asynccontextmanager
    async def lifespan(parent_app):
        async with mcp.session_manager.run():
            yield

    routes = [
        Route("/", endpoint=root, methods=["GET"]),
        Route("/health", endpoint=health, methods=["GET"]),

        # OAuth 2.1 Authorization Server endpoints
        Route("/.well-known/oauth-authorization-server",
              endpoint=oauth_server.authorization_server_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource",
              endpoint=oauth_server.protected_resource_metadata, methods=["GET"]),
        Route("/oauth/register",       endpoint=oauth_server.register,        methods=["POST"]),
        Route("/oauth/authorize",      endpoint=oauth_server.authorize,       methods=["GET"]),
        Route("/oauth/google-callback", endpoint=oauth_server.google_callback, methods=["GET"]),
        Route("/oauth/token",          endpoint=oauth_server.token,           methods=["POST"]),
        Route("/oauth/revoke",         endpoint=oauth_server.revoke,          methods=["POST"]),

        # Legacy: per-account add flow (used by gmail_authenticate tool for additional accounts)
        Route("/oauth/callback", endpoint=oauth_callback, methods=["GET"]),

        # Signed, time-limited attachment downloads
        Route("/files/{token}", endpoint=serve_file, methods=["GET"]),
        Route("/files/{token}/{fname:path}", endpoint=serve_file, methods=["GET"]),

        # MCP streamable-HTTP endpoint last (catches everything else)
        Mount("/", app=mcp_app),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(OAuthAccessTokenMiddleware)

    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")


def main() -> None:
    if TRANSPORT == "stdio":
        _run_stdio()
    elif TRANSPORT == "http":
        _run_http()
    else:
        log(f"Unknown GMAIL_MCP_TRANSPORT='{TRANSPORT}' — must be 'stdio' or 'http'.")
        sys.exit(2)


if __name__ == "__main__":
    main()

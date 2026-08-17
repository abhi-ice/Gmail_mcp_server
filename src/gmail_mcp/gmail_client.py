"""
Gmail API wrapper — all email operations.
Every public function takes (user_id, alias). Auth is per-user.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from .auth import (
    get_gmail_service,
    resolve_label_ids_to_names,
    resolve_label_names_to_ids,
)
from .utils import (
    extract_attachment_info,
    extract_body,
    format_email_headers,
)


def search_emails(user_id: str, alias: str, query: str, max_results: int = 10) -> list[dict]:
    service = get_gmail_service(user_id, alias)

    list_result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )

    messages = list_result.get("messages", [])
    if not messages:
        return []

    results = []
    for msg_ref in messages:
        msg_id = msg_ref["id"]
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Date", "Cc"],
                )
                .execute()
            )

            headers = format_email_headers(msg.get("payload", {}).get("headers", []))
            label_ids = msg.get("labelIds", [])
            label_names = resolve_label_ids_to_names(user_id, alias, label_ids)
            snippet = msg.get("snippet", "")
            if len(snippet) > 150:
                snippet = snippet[:147] + "..."

            results.append(
                {
                    "id": msg_id,
                    "subject": headers.get("Subject", "(no subject)"),
                    "from": headers.get("From", ""),
                    "to": headers.get("To", ""),
                    "cc": headers.get("Cc", ""),
                    "date": headers.get("Date", ""),
                    "snippet": snippet,
                    "labels": label_names,
                    "threadId": msg.get("threadId", ""),
                }
            )
        except Exception as e:
            print(f"[gmail-mcp] Error fetching message {msg_id}: {e}", file=sys.stderr)
            continue

    return results


def read_email(user_id: str, alias: str, message_id: str) -> dict:
    service = get_gmail_service(user_id, alias)

    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

    payload = msg.get("payload", {})
    headers = format_email_headers(payload.get("headers", []))
    label_ids = msg.get("labelIds", [])
    label_names = resolve_label_ids_to_names(user_id, alias, label_ids)

    return {
        "id": message_id,
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "cc": headers.get("Cc", ""),
        "date": headers.get("Date", ""),
        "labels": label_names,
        "body": extract_body(payload),
        "attachments": extract_attachment_info(payload),
        "threadId": msg.get("threadId", ""),
    }


def _decode_header_value(value: str) -> str:
    """RFC 2047 -> readable text (subjects are often =?UTF-8?B?...?= encoded)."""
    from email.header import decode_header, make_header

    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _eml_meta(binary: bytes) -> tuple[str, str]:
    """Pull (subject, YYYY-MM-DD) straight out of the RFC 822 bytes.

    Parsing locally avoids a second Gmail API round-trip just to name the file.
    """
    import email as _email
    from email.utils import parsedate_to_datetime

    try:
        msg = _email.message_from_bytes(binary)
    except Exception:
        return "(no subject)", ""

    subject = _decode_header_value(msg.get("Subject", "")).strip() or "(no subject)"
    stamp = ""
    raw_date = msg.get("Date", "")
    if raw_date:
        try:
            stamp = parsedate_to_datetime(raw_date).strftime("%Y-%m-%d")
        except Exception:
            stamp = ""
    return subject, stamp


def _eml_filename(subject: str, stamp: str) -> str:
    """Build a filesystem-safe '<date> <subject>.eml' name."""
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", subject)
    base = re.sub(r"\s+", " ", base).strip(" .")[:120].strip()
    if not base:
        base = "email"
    return f"{stamp} {base}.eml" if stamp else f"{base}.eml"


def export_email_raw(user_id: str, alias: str, message_id: str) -> dict:
    """
    Fetch a message as RFC 822 (.eml) — the complete original email: every
    header, both the plain-text and HTML bodies, inline images and all
    attachments, byte-for-byte as it was received.

    Gmail returns this via format="raw" as base64url text, which we decode back
    into the real .eml bytes. A generous ceiling (MAX_ATTACHMENT_MB) guards the
    container against a pathologically large message.

    Returns dict: {filename, mime_type, size_bytes, binary, subject, date}.
    """
    from .config import MAX_ATTACHMENT_MB

    service = get_gmail_service(user_id, alias)

    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute()
    )

    raw = msg.get("raw") or ""
    if not raw:
        raise RuntimeError(
            "Gmail returned no raw content for this message. "
            "Confirm the message_id is correct and belongs to this account."
        )

    binary = base64.urlsafe_b64decode(raw.encode("ascii"))

    ceiling = MAX_ATTACHMENT_MB * 1024 * 1024
    if len(binary) > ceiling:
        raise RuntimeError(
            f"Message is {len(binary) / 1024 / 1024:.1f} MB, over the "
            f"{MAX_ATTACHMENT_MB} MB server ceiling. Raise "
            f"GMAIL_MCP_MAX_ATTACHMENT_MB if you need larger exports."
        )

    subject, stamp = _eml_meta(binary)
    return {
        "filename": _eml_filename(subject, stamp),
        "mime_type": "message/rfc822",
        "size_bytes": len(binary),
        "binary": binary,
        "subject": subject,
        "date": stamp,
    }


def resolve_attachment(
    user_id: str,
    alias: str,
    message_id: str,
    attachment_id: str,
) -> dict:
    """
    Fetch attachment bytes and discover its real filename + MIME from the message.

    NOTE: Gmail regenerates attachment_id values on every messages.get()
    call — they're ephemeral. The same attachment downloaded twice will have
    two different attachment_ids. The attachments().get() endpoint tolerates
    old IDs (the download still works), but we can't match by attachmentId
    in a fresh message-tree walk.

    Strategy: collect every attachment-shaped part from the tree (filename,
    mime, body.size) in document order. Download the bytes via the passed
    attachment_id (Gmail accepts it). Then match the downloaded size to the
    tree's reported body.size to find the right filename.

    No per-call size cap — only a generous safety ceiling (MAX_ATTACHMENT_MB)
    so a malformed/huge payload can't OOM the container. Gmail's own
    attachment limits sit well under that ceiling.

    Returns dict: {filename, mime_type, size_bytes, binary}.
    """
    from .config import MAX_ATTACHMENT_MB

    service = get_gmail_service(user_id, alias)

    # Walk the tree to enumerate attachments (with FRESH attachment_ids that
    # we mostly ignore, but we keep filename/mime/size per part).
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

    tree_attachments: list[dict] = []

    def _walk(part: dict) -> None:
        body = part.get("body", {})
        filename = part.get("filename") or ""
        if filename:  # parts with filenames are attachments (vs inline body)
            tree_attachments.append({
                "filename": filename,
                "mimeType": part.get("mimeType") or "",
                "size": int(body.get("size") or 0),
                "fresh_attachment_id": body.get("attachmentId") or "",
            })
        for sub in part.get("parts", []):
            _walk(sub)

    _walk(msg.get("payload", {}))

    # Download the bytes via the caller's attachment_id (works even if stale)
    result = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = result.get("data", "")
    if not data:
        raise RuntimeError(
            "Attachment returned no data. The attachment_id may be invalid."
        )

    padded = data + "=" * (-len(data) % 4)
    binary = base64.urlsafe_b64decode(padded)

    ceiling = MAX_ATTACHMENT_MB * 1024 * 1024
    if len(binary) > ceiling:
        raise ValueError(
            f"Attachment is {len(binary)/1024/1024:.1f} MB, above the server safety "
            f"ceiling of {MAX_ATTACHMENT_MB} MB (GMAIL_MCP_MAX_ATTACHMENT_MB). "
            f"This is larger than Gmail normally allows; if it's legitimate, raise the env var."
        )

    # Match by size to recover filename + mime from the tree
    matches_by_size = [a for a in tree_attachments if a["size"] == len(binary)]
    filename = ""
    mime = ""
    if len(matches_by_size) == 1:
        # Unambiguous size match — trust it
        filename = matches_by_size[0]["filename"]
        mime = matches_by_size[0]["mimeType"]
    elif len(matches_by_size) > 1:
        # Multiple attachments with the same size — take the first; user can
        # tell them apart by content. (Rare in practice.)
        filename = matches_by_size[0]["filename"]
        mime = matches_by_size[0]["mimeType"]
        print(
            f"[gmail-mcp] resolve_attachment: {len(matches_by_size)} attachments "
            f"share size {len(binary)} bytes; using first match '{filename}'.",
            file=sys.stderr,
        )
    else:
        # No size match. Could be a body part inlined as attachment, or
        # Gmail's reported size differs slightly from the decoded byte length.
        # Fall back to magic-byte detection.
        print(
            f"[gmail-mcp] resolve_attachment: no size match for {len(binary)} bytes. "
            f"Tree had: {[(a['filename'], a['size']) for a in tree_attachments]}",
            file=sys.stderr,
        )

    if not filename:
        filename = _guess_filename(binary)
    if not mime:
        mime = _guess_mime(binary)

    return {
        "filename": filename,
        "mime_type": mime,
        "size_bytes": len(binary),
        "binary": binary,
    }


def _guess_filename(binary: bytes) -> str:
    """Best-effort filename when message walker didn't find the part."""
    if binary[:5] == b"%PDF-":
        return "attachment.pdf"
    if binary[:4] == b"PK\x03\x04":
        # ZIP-based — could be DOCX, XLSX, PPTX, or plain ZIP
        # Try to peek at the ZIP central directory for hints
        try:
            import zipfile
            from io import BytesIO
            with zipfile.ZipFile(BytesIO(binary)) as zf:
                names = zf.namelist()
                if "word/document.xml" in names:
                    return "attachment.docx"
                if "xl/workbook.xml" in names:
                    return "attachment.xlsx"
                if "ppt/presentation.xml" in names:
                    return "attachment.pptx"
        except Exception:
            pass
        return "attachment.zip"
    if binary[:3] == b"\xff\xd8\xff":
        return "attachment.jpg"
    if binary[:8] == b"\x89PNG\r\n\x1a\n":
        return "attachment.png"
    return "attachment.bin"


def _guess_mime(binary: bytes) -> str:
    """Best-effort MIME from magic bytes."""
    if binary[:5] == b"%PDF-":
        return "application/pdf"
    if binary[:4] == b"PK\x03\x04":
        try:
            import zipfile
            from io import BytesIO
            with zipfile.ZipFile(BytesIO(binary)) as zf:
                names = zf.namelist()
                if "word/document.xml" in names:
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if "xl/workbook.xml" in names:
                    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if "ppt/presentation.xml" in names:
                    return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        except Exception:
            pass
        return "application/zip"
    if binary[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if binary[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Outbound mail: shared MIME builder (used by both draft and send)
# ---------------------------------------------------------------------------

# Gmail rejects messages over 25 MB once base64-encoded. Encoding inflates by
# ~4/3, so cap the raw payload below that to fail here with a clear message
# rather than getting an opaque 400 back from the API.
MAX_OUTBOUND_ATTACHMENT_BYTES = 24 * 1024 * 1024


def _attach_file(msg: MIMEMultipart, path: Path) -> int:
    """Attach one file to msg. Returns its size in bytes."""
    data = path.read_bytes()
    ctype, encoding = mimetypes.guess_type(str(path))
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)

    part = MIMEBase(maintype, subtype)
    part.set_payload(data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=path.name)
    msg.attach(part)
    return len(data)


def _build_outbound_message(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachments: Optional[list[str]] = None,
    html_body: Optional[str] = None,
):
    """
    Build a MIME message. Plain MIMEText when there is nothing to attach and no
    HTML alternative, MIMEMultipart otherwise — Gmail is happier with a simple
    single-part message when multipart buys nothing.
    """
    paths: list[Path] = []
    for raw_path in (attachments or []):
        p = Path(raw_path).expanduser()
        if not p.exists():
            raise ValueError(f"Attachment not found: {p}")
        if not p.is_file():
            raise ValueError(f"Attachment is not a file: {p}")
        paths.append(p)

    if not paths and not html_body:
        msg = MIMEText(body, "plain", "utf-8")
    else:
        msg = MIMEMultipart("mixed")
        if html_body:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body, "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)
        else:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        total = 0
        for p in paths:
            total += _attach_file(msg, p)
        if total > MAX_OUTBOUND_ATTACHMENT_BYTES:
            raise ValueError(
                f"Attachments total {total / 1024 / 1024:.1f} MB, over Gmail's ~25 MB "
                f"per-message ceiling. Send fewer or smaller files, or link them instead."
            )

    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    return msg


def _apply_threading(service, msg, reply_to_message_id: Optional[str]) -> Optional[str]:
    """Set In-Reply-To/References on msg. Returns the thread id, if any."""
    if not reply_to_message_id:
        return None
    try:
        original = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=reply_to_message_id,
                format="metadata",
                metadataHeaders=["Message-ID", "Subject", "References"],
            )
            .execute()
        )
        orig_headers = format_email_headers(original.get("payload", {}).get("headers", []))
        orig_message_id = orig_headers.get("Message-ID", "")
        orig_references = orig_headers.get("References", "")

        if orig_message_id:
            msg["In-Reply-To"] = orig_message_id
            msg["References"] = (
                f"{orig_references} {orig_message_id}".strip()
                if orig_references
                else orig_message_id
            )
        return original.get("threadId")
    except Exception as e:
        print(
            f"[gmail-mcp] Warning: could not fetch original message for threading: {e}",
            file=sys.stderr,
        )
        return None


def create_draft(
    user_id: str,
    alias: str,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    attachments: Optional[list[str]] = None,
    html_body: Optional[str] = None,
) -> dict:
    service = get_gmail_service(user_id, alias)

    msg = _build_outbound_message(to, subject, body, cc, bcc, attachments, html_body)
    thread_id = _apply_threading(service, msg, reply_to_message_id)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    draft_body: dict = {"message": {"raw": raw}}
    if thread_id:
        draft_body["message"]["threadId"] = thread_id

    result = service.users().drafts().create(userId="me", body=draft_body).execute()

    return {
        "draft_id": result.get("id", ""),
        "message_id": result.get("message", {}).get("id", ""),
        "thread_id": result.get("message", {}).get("threadId", ""),
    }


def send_message(
    user_id: str,
    alias: str,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    attachments: Optional[list[str]] = None,
    html_body: Optional[str] = None,
) -> dict:
    """Send an email immediately. Unlike create_draft this cannot be undone."""
    service = get_gmail_service(user_id, alias)

    msg = _build_outbound_message(to, subject, body, cc, bcc, attachments, html_body)
    thread_id = _apply_threading(service, msg, reply_to_message_id)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_body: dict = {"raw": raw}
    if thread_id:
        send_body["threadId"] = thread_id

    result = service.users().messages().send(userId="me", body=send_body).execute()

    return {
        "message_id": result.get("id", ""),
        "thread_id": result.get("threadId", ""),
        "label_ids": result.get("labelIds", []),
    }


def modify_email(
    user_id: str,
    alias: str,
    message_id: str,
    action: str,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
) -> dict:
    service = get_gmail_service(user_id, alias)

    add_ids: list[str] = []
    remove_ids: list[str] = []

    action_map = {
        "trash":         (["TRASH"], []),
        "untrash":       ([], ["TRASH"]),
        "archive":       ([], ["INBOX"]),
        "move_to_inbox": (["INBOX"], []),
        "mark_read":     ([], ["UNREAD"]),
        "mark_unread":   (["UNREAD"], []),
        "star":          (["STARRED"], []),
        "unstar":        ([], ["STARRED"]),
    }

    if action in action_map:
        a_add, a_remove = action_map[action]
        add_ids.extend(a_add)
        remove_ids.extend(a_remove)

    if add_labels:
        add_ids.extend(resolve_label_names_to_ids(user_id, alias, add_labels))
    if remove_labels:
        remove_ids.extend(resolve_label_names_to_ids(user_id, alias, remove_labels))

    add_ids = list(dict.fromkeys(add_ids))
    remove_ids = list(dict.fromkeys(remove_ids))

    result = (
        service.users()
        .messages()
        .modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": add_ids, "removeLabelIds": remove_ids},
        )
        .execute()
    )

    updated_label_ids = result.get("labelIds", [])
    updated_label_names = resolve_label_ids_to_names(user_id, alias, updated_label_ids)

    return {
        "message_id": message_id,
        "action": action,
        "labels": updated_label_names,
    }


def list_labels(user_id: str, alias: str) -> list[dict]:
    service = get_gmail_service(user_id, alias)

    labels_result = service.users().labels().list(userId="me").execute()
    labels = labels_result.get("labels", [])

    detailed: list[dict] = []
    for lbl in labels:
        try:
            detail = service.users().labels().get(userId="me", id=lbl["id"]).execute()
            detailed.append(
                {
                    "id": detail.get("id", ""),
                    "name": detail.get("name", ""),
                    "type": detail.get("type", "user"),
                    "total_messages": detail.get("messagesTotal", 0),
                    "unread_messages": detail.get("messagesUnread", 0),
                }
            )
        except Exception as e:
            print(f"[gmail-mcp] Warning: could not get detail for label {lbl.get('id')}: {e}", file=sys.stderr)
            detailed.append(
                {
                    "id": lbl.get("id", ""),
                    "name": lbl.get("name", ""),
                    "type": lbl.get("type", "user"),
                    "total_messages": 0,
                    "unread_messages": 0,
                }
            )

    system_labels = sorted([l for l in detailed if l["type"] == "system"], key=lambda x: x["name"])
    user_labels = sorted([l for l in detailed if l["type"] != "system"], key=lambda x: x["name"])

    return system_labels + user_labels

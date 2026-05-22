"""
Gmail API wrapper — all email operations.
Every public function takes (user_id, alias). Auth is per-user.
"""

from __future__ import annotations

import base64
import os
import sys
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


def read_attachment(
    user_id: str,
    alias: str,
    message_id: str,
    attachment_id: str,
    max_size_mb: int = 10,
) -> dict:
    """
    Fetch attachment bytes and discover filename/mime from the message.
    Returns dict: {filename, mime_type, size_bytes, binary}.
    Raises ValueError if the attachment exceeds max_size_mb.
    """
    service = get_gmail_service(user_id, alias)

    # Fetch message metadata to find filename + mime for this attachment
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

    found = {"filename": "", "mimeType": "", "size": 0}
    all_attachments_seen: list[dict] = []  # for diagnostic logging

    def _walk(part: dict) -> bool:
        body = part.get("body", {})
        bid = body.get("attachmentId")
        if bid:
            all_attachments_seen.append({
                "id_prefix": bid[:24],
                "filename": part.get("filename", ""),
                "mimeType": part.get("mimeType", ""),
            })
        if bid and bid == attachment_id:
            found["filename"] = part.get("filename") or ""
            found["mimeType"] = part.get("mimeType") or ""
            found["size"] = int(body.get("size") or 0)
            return True
        for sub in part.get("parts", []):
            if _walk(sub):
                return True
        return False

    matched = _walk(msg.get("payload", {}))

    if not matched:
        print(
            f"[gmail-mcp] read_attachment: target id {attachment_id[:24]}... "
            f"NOT FOUND in message tree. Saw {len(all_attachments_seen)} attachment(s): "
            f"{all_attachments_seen}",
            file=sys.stderr,
        )

    max_bytes = max_size_mb * 1024 * 1024
    if found["size"] > max_bytes:
        raise ValueError(
            f"Attachment is {found['size']} bytes ({found['size']/1024/1024:.1f} MB), "
            f"exceeds max_size_mb={max_size_mb}. Use gmail_download_attachment for larger files."
        )

    # Fetch the bytes
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

    # Fallback: if walker didn't find the part, infer filename/mime from magic bytes
    if not found["filename"]:
        found["filename"] = _guess_filename(binary)
    if not found["mimeType"]:
        found["mimeType"] = _guess_mime(binary)

    return {
        "filename": found["filename"],
        "mime_type": found["mimeType"],
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


def download_attachment(
    user_id: str,
    alias: str,
    message_id: str,
    attachment_id: str,
    filename: str,
    save_dir: Optional[str] = None,
) -> dict:
    """Fetch attachment bytes, decode, write to disk. Returns path + size."""
    service = get_gmail_service(user_id, alias)

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
            "Attachment returned no data. The attachment_id may be invalid or the attachment may be empty."
        )

    padded = data + "=" * (-len(data) % 4)
    binary = base64.urlsafe_b64decode(padded)

    safe_name = os.path.basename(filename).strip()
    if not safe_name or safe_name in (".", ".."):
        raise ValueError("filename is invalid after sanitization.")

    target_dir = Path(save_dir).expanduser().resolve() if save_dir else Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    target_path.write_bytes(binary)

    return {
        "path": str(target_path),
        "filename": safe_name,
        "size_bytes": len(binary),
    }


def create_draft(
    user_id: str,
    alias: str,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
) -> dict:
    service = get_gmail_service(user_id, alias)

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc

    thread_id: Optional[str] = None

    if reply_to_message_id:
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
            thread_id = original.get("threadId")

            if orig_message_id:
                msg["In-Reply-To"] = orig_message_id
                references = (
                    f"{orig_references} {orig_message_id}".strip()
                    if orig_references
                    else orig_message_id
                )
                msg["References"] = references
        except Exception as e:
            print(
                f"[gmail-mcp] Warning: could not fetch original message for threading: {e}",
                file=sys.stderr,
            )

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

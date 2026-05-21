"""
Gmail API wrapper — all email operations.
All public functions take an account alias and call through the auth module.
"""

import base64
import os
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

from .auth import (
    get_gmail_service,
    get_label_cache,
    resolve_label_ids_to_names,
    resolve_label_names_to_ids,
)
from .utils import (
    extract_attachment_info,
    extract_body,
    format_email_headers,
    format_size,
)


def search_emails(alias: str, query: str, max_results: int = 10) -> list[dict]:
    """
    Search emails using Gmail query syntax.
    Returns list of message dicts with metadata.
    """
    service = get_gmail_service(alias)

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
            label_names = resolve_label_ids_to_names(alias, label_ids)
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
            # Skip bad messages rather than failing the whole search
            continue

    return results


def read_email(alias: str, message_id: str) -> dict:
    """
    Fetch full email content including body and attachment metadata.
    """
    service = get_gmail_service(alias)

    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

    payload = msg.get("payload", {})
    headers = format_email_headers(payload.get("headers", []))
    label_ids = msg.get("labelIds", [])
    label_names = resolve_label_ids_to_names(alias, label_ids)

    body = extract_body(payload)
    attachments = extract_attachment_info(payload)

    return {
        "id": message_id,
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "cc": headers.get("Cc", ""),
        "date": headers.get("Date", ""),
        "labels": label_names,
        "body": body,
        "attachments": attachments,
        "threadId": msg.get("threadId", ""),
    }


def create_draft(
    alias: str,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
) -> dict:
    """
    Create an email draft. Supports reply threading.
    Returns dict with draft id and message id.
    """
    service = get_gmail_service(alias)

    # Build MIME message
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
            orig_headers = format_email_headers(
                original.get("payload", {}).get("headers", [])
            )
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

    # Encode
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
    alias: str,
    message_id: str,
    action: str,
    add_labels: Optional[list[str]] = None,
    remove_labels: Optional[list[str]] = None,
) -> dict:
    """
    Modify an email's labels/status.
    Returns the updated message's label list.
    """
    service = get_gmail_service(alias)

    add_ids: list[str] = []
    remove_ids: list[str] = []

    # Map action to label operations
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

    # Resolve custom labels
    if add_labels:
        add_ids.extend(resolve_label_names_to_ids(alias, add_labels))
    if remove_labels:
        remove_ids.extend(resolve_label_names_to_ids(alias, remove_labels))

    # Deduplicate
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
    updated_label_names = resolve_label_ids_to_names(alias, updated_label_ids)

    return {
        "message_id": message_id,
        "action": action,
        "labels": updated_label_names,
    }


def download_attachment(
    alias: str,
    message_id: str,
    attachment_id: str,
    filename: str,
    save_dir: Optional[str] = None,
) -> dict:
    """
    Download a single attachment to disk by attachment ID.
    Returns dict with keys: path, filename, size_bytes.
    Overwrites if a file with the same name already exists in save_dir.
    """
    service = get_gmail_service(alias)

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


def list_labels(alias: str) -> list[dict]:
    """
    List all labels with message counts.
    Returns list of label dicts sorted: system labels first, then user labels alphabetically.
    """
    service = get_gmail_service(alias)

    labels_result = service.users().labels().list(userId="me").execute()
    labels = labels_result.get("labels", [])

    detailed: list[dict] = []
    for lbl in labels:
        try:
            detail = (
                service.users()
                .labels()
                .get(userId="me", id=lbl["id"])
                .execute()
            )
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

    system_labels = sorted(
        [l for l in detailed if l["type"] == "system"], key=lambda x: x["name"]
    )
    user_labels = sorted(
        [l for l in detailed if l["type"] != "system"], key=lambda x: x["name"]
    )

    return system_labels + user_labels

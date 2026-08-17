"""Pydantic input models for all Gmail MCP tools."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AuthenticateInput(BaseModel):
    alias: str = Field(
        ...,
        min_length=1,
        max_length=30,
        description="Friendly name for the account (e.g., 'company1'). Lowercase alphanumeric with hyphens only.",
    )
    email: str = Field(
        ...,
        description="Gmail address to authenticate (e.g., user@company1.com).",
    )
    description: Optional[str] = Field(
        None,
        max_length=100,
        description="Human-readable description (e.g., 'Main construction company email').",
    )

    @field_validator("alias")
    @classmethod
    def alias_format(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z0-9][a-z0-9\-]*$", v):
            raise ValueError("alias must be lowercase alphanumeric with hyphens only, and must start with a letter or digit")
        return v

    @field_validator("email")
    @classmethod
    def email_format(cls, v: str) -> str:
        import re
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("email must be a valid email address")
        return v.lower().strip()


class SearchEmailsInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias to search in (e.g., 'company1').",
    )
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Gmail search query. Examples: "
            "'from:client@example.com', "
            "'subject:invoice after:2024/01/01', "
            "'is:unread in:inbox', "
            "'has:attachment label:important'."
        ),
    )
    max_results: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of results to return (1–50, default 10).",
    )


class ReadEmailInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias.",
    )
    message_id: str = Field(
        ...,
        description="Email message ID from search results.",
    )


class DraftEmailInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias to create draft in.",
    )
    to: str = Field(
        ...,
        description="Recipient email address(es), comma-separated.",
    )
    subject: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Email subject line.",
    )
    body: str = Field(
        ...,
        min_length=1,
        description="Email body text (plain text).",
    )
    cc: Optional[str] = Field(
        None,
        description="CC recipients, comma-separated.",
    )
    bcc: Optional[str] = Field(
        None,
        description="BCC recipients, comma-separated.",
    )
    reply_to_message_id: Optional[str] = Field(
        None,
        description="If this is a reply, provide the original message ID to set proper threading headers (In-Reply-To, References, threadId).",
    )
    attachments: Optional[list[str]] = Field(
        None,
        max_length=25,
        description="Absolute paths to local files to attach. Combined size must stay under ~25 MB.",
    )
    html_body: Optional[str] = Field(
        None,
        description="Optional HTML version of the body. When set, the message is multipart/alternative and 'body' is the plain-text fallback.",
    )


class SendEmailInput(DraftEmailInput):
    """Same shape as a draft — sending is the same message, a different endpoint."""
    pass


class EmailAction(str, Enum):
    trash = "trash"
    untrash = "untrash"
    archive = "archive"
    move_to_inbox = "move_to_inbox"
    mark_read = "mark_read"
    mark_unread = "mark_unread"
    star = "star"
    unstar = "unstar"


class ModifyEmailInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias.",
    )
    message_id: str = Field(
        ...,
        description="Email message ID.",
    )
    action: EmailAction = Field(
        ...,
        description=(
            "Action to perform. One of: 'trash', 'untrash', 'archive', 'move_to_inbox', "
            "'mark_read', 'mark_unread', 'star', 'unstar'."
        ),
    )
    add_labels: Optional[list[str]] = Field(
        None,
        description="Additional label names or IDs to add.",
    )
    remove_labels: Optional[list[str]] = Field(
        None,
        description="Label names or IDs to remove.",
    )


class ListLabelsInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias.",
    )


class SearchContactsInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias.",
    )
    query: str = Field(
        ...,
        min_length=1,
        description="Name, email, or phone number to search for.",
    )


class DownloadAttachmentInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias.",
    )
    message_id: str = Field(
        ...,
        min_length=1,
        description="Email message ID (from gmail_search_emails or gmail_read_email).",
    )
    attachment_id: str = Field(
        ...,
        min_length=1,
        description="Attachment ID (from gmail_read_email output).",
    )
    filename: Optional[str] = Field(
        None,
        max_length=255,
        description="Optional filename to use. If omitted, the attachment's real "
                    "filename is auto-detected. Path components are stripped for safety.",
    )


class ExportEmailInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias.",
    )
    message_id: str = Field(
        ...,
        min_length=1,
        description="Email message ID (from gmail_search_emails or gmail_read_email).",
    )
    filename: Optional[str] = Field(
        None,
        max_length=255,
        description="Optional filename for the .eml file. If omitted, one is built "
                    "from the message date and subject. Path components are stripped "
                    "for safety and a .eml extension is enforced.",
    )


class ReadAttachmentInput(BaseModel):
    account: str = Field(
        ...,
        description="Account alias.",
    )
    message_id: str = Field(
        ...,
        min_length=1,
        description="Email message ID (from gmail_search_emails or gmail_read_email).",
    )
    attachment_id: str = Field(
        ...,
        min_length=1,
        description="Attachment ID (from gmail_read_email output).",
    )

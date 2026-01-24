"""Meta-tools for LLM agents - consolidated interface with action-based routing."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Import existing tools for delegation
from tools.forward_mail import forward_mail as _forward_mail
from tools.list_mails import list_mails as _list_mails
from tools.read_mail import read_mail as _read_mail
from tools.reply_all_mail import reply_all_mail as _reply_all_mail
from tools.reply_mail import reply_mail as _reply_mail
from tools.search_mail import search_mail as _search_mail
from tools.send_mail import send_mail as _send_mail
from utils.config import DEFAULT_LIST_LIMIT


# ============ Help Response ============
class ActionInfo(BaseModel):
    """Information about an action."""

    model_config = ConfigDict(extra="forbid")
    description: str
    required_params: list[str]
    optional_params: list[str]


class HelpResponse(BaseModel):
    """Help response listing available actions."""

    model_config = ConfigDict(extra="forbid")
    tool_name: str
    description: str
    actions: dict[str, ActionInfo]


# ============ Result Models ============
class SendResult(BaseModel):
    """Result from send/reply/forward mail operations."""

    model_config = ConfigDict(extra="forbid")
    success: bool
    mail_id: str | None = None
    recipients_count: int | None = None
    message: str
    error: str | None = None


class MailDetailsResult(BaseModel):
    """Result from reading a mail."""

    model_config = ConfigDict(extra="forbid")
    mail: dict[str, Any]


class MailSummaryItem(BaseModel):
    """Summary of a single mail for list/search results."""

    model_config = ConfigDict(extra="forbid")
    mail_id: str
    timestamp: str
    from_email: str
    to: list[str]
    subject: str
    thread_id: str | None = None
    in_reply_to: str | None = None


class MailListResult(BaseModel):
    """Result from listing or searching mails."""

    model_config = ConfigDict(extra="forbid")
    mails: list[MailSummaryItem]
    count: int
    page: int | None = None
    limit: int
    has_more: bool


# ============ Input Model ============
class MailInput(BaseModel):
    """Input for mail meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "help",
        "send",
        "read",
        "list",
        "search",
        "reply",
        "reply_all",
        "forward",
    ] = Field(..., description="Action to perform")

    # Mail identification (for read/reply/reply_all/forward)
    mail_id: str | None = Field(None, description="Mail ID for read/reply/forward")

    # Send/reply/forward fields
    from_email: str | None = Field(None, description="Sender email address")
    to_email: str | list[str] | None = Field(
        None, description="Recipient email address(es)"
    )
    subject: str | None = Field(None, description="Email subject line")
    body: str | None = Field(
        None, description="Email or message body content. REQUIRED for send."
    )
    cc: str | list[str] | None = Field(
        None, description="CC recipients, comma-separated emails."
    )
    bcc: str | list[str] | None = Field(
        None, description="BCC recipients, comma-separated emails."
    )
    attachments: list[str] | None = Field(None, description="File paths to attach")
    body_format: Literal["plain", "html"] | None = Field(
        None, description="Body format: 'plain' or 'html'"
    )

    # Threading fields (for send)
    thread_id: str | None = Field(None, description="Thread identifier for grouping")
    in_reply_to: str | None = Field(None, description="Message-ID being replied to")
    references: list[str] | None = Field(
        None, description="List of referenced Message-IDs"
    )

    # List/search pagination
    page: int | None = Field(None, description="Page number (0-indexed)", ge=0)
    limit: int | None = Field(None, description="Results per page", ge=1, le=100)
    offset: int | None = Field(None, description="Number of mails to skip", ge=0)

    # Search filters
    search_from: str | None = Field(None, description="Filter by sender email")
    search_to: str | None = Field(None, description="Filter by recipient email")
    search_subject: str | None = Field(
        None, description="Filter by subject (partial match)"
    )
    after_date: str | None = Field(
        None, description="Filter emails after this date (YYYY-MM-DD or ISO format)"
    )
    before_date: str | None = Field(
        None, description="Filter emails before this date (YYYY-MM-DD or ISO format)"
    )
    search_thread_id: str | None = Field(None, description="Filter by thread ID")


# ============ Output Model ============
class MailOutput(BaseModel):
    """Output for mail meta-tool."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        ...,
        description="The operation to perform. REQUIRED. Call with action='help' first.",
    )
    error: str | None = Field(None, description="Error message if failed")

    # Discovery
    help: HelpResponse | None = None

    # Action-specific results
    send: SendResult | None = None
    read: MailDetailsResult | None = None
    list: MailListResult | None = None
    search: MailListResult | None = None
    reply: SendResult | None = None
    reply_all: SendResult | None = None
    forward: SendResult | None = None


# ============ Help Definition ============
MAIL_HELP = HelpResponse(
    tool_name="mail",
    description="Mail operations: send, read, list, search, reply, reply_all, and forward emails.",
    actions={
        "help": ActionInfo(
            description="List all available actions",
            required_params=[],
            optional_params=[],
        ),
        "send": ActionInfo(
            description="Send a new email",
            required_params=["to_email", "subject", "body"],
            optional_params=[
                "from_email",
                "cc",
                "bcc",
                "attachments",
                "body_format",
                "thread_id",
                "in_reply_to",
                "references",
            ],
        ),
        "read": ActionInfo(
            description="Read a mail by its Message-ID",
            required_params=["mail_id"],
            optional_params=[],
        ),
        "list": ActionInfo(
            description="List emails with pagination (most recent first)",
            required_params=[],
            optional_params=["page", "limit", "offset"],
        ),
        "search": ActionInfo(
            description="Search emails by sender, recipient, subject, date range, or thread",
            required_params=[],
            optional_params=[
                "search_from",
                "search_to",
                "search_subject",
                "after_date",
                "before_date",
                "search_thread_id",
                "limit",
            ],
        ),
        "reply": ActionInfo(
            description="Reply to an email (sender only), preserving thread",
            required_params=["mail_id", "body"],
            optional_params=["attachments", "body_format"],
        ),
        "reply_all": ActionInfo(
            description="Reply to all recipients of an email, preserving thread",
            required_params=["mail_id", "body"],
            optional_params=["attachments", "body_format"],
        ),
        "forward": ActionInfo(
            description="Forward an email to new recipients",
            required_params=["mail_id", "to_email"],
            optional_params=["body", "cc", "bcc", "attachments", "body_format"],
        ),
    },
)


# ============ Result Parsing Helpers ============
def _parse_send_result(result_str: str) -> SendResult:
    """Parse the string result from send_mail into a SendResult."""
    if result_str.startswith("Error:"):
        return SendResult(
            success=False,
            mail_id=None,
            recipients_count=None,
            message="Send failed",
            error=result_str[7:].strip(),
        )

    if "Mail sent successfully" in result_str:
        mail_id = None
        recipients_count = None

        if "Mail ID:" in result_str:
            try:
                id_part = result_str.split("Mail ID:")[1]
                mail_id = id_part.split(",")[0].strip()
            except (IndexError, ValueError):
                pass

        if "Recipients:" in result_str:
            try:
                count_part = result_str.split("Recipients:")[1]
                recipients_count = int(count_part.strip())
            except (IndexError, ValueError):
                pass

        return SendResult(
            success=True,
            mail_id=mail_id,
            recipients_count=recipients_count,
            message="Mail sent successfully",
            error=None,
        )

    # Fallback for unexpected format
    return SendResult(
        success=False,
        mail_id=None,
        recipients_count=None,
        message=result_str,
        error=None,
    )


def _parse_mail_list_result(
    result_str: str, limit: int, page: int | None = None
) -> MailListResult:
    """Parse the string result from list_mails/search_mail into a MailListResult."""
    import re

    if result_str.startswith("Failed to list mails:"):
        return MailListResult(
            mails=[],
            count=0,
            page=page,
            limit=limit,
            has_more=False,
        )

    if result_str == "No emails found":
        return MailListResult(
            mails=[],
            count=0,
            page=page,
            limit=limit,
            has_more=False,
        )

    mails: list[MailSummaryItem] = []
    entries = re.split(r"\n\d+\. ", result_str)

    for entry in entries[1:]:
        lines = entry.strip().split("\n")
        mail_data: dict[str, Any] = {}

        for line in lines:
            if line.startswith("Mail ID:"):
                mail_data["mail_id"] = line[8:].strip()
            elif line.startswith("Timestamp:"):
                mail_data["timestamp"] = line[10:].strip()
            elif line.startswith("From:"):
                mail_data["from_email"] = line[5:].strip()
            elif line.startswith("To:"):
                to_part = line[3:].strip()
                if " (" in to_part:
                    to_part = to_part.split(" (")[0]
                mail_data["to"] = [e.strip() for e in to_part.split(",")]
            elif line.startswith("Subject:"):
                mail_data["subject"] = line[8:].strip()
            elif line.startswith("Thread:"):
                mail_data["thread_id"] = line[7:].strip()
            elif line.startswith("In Reply To:"):
                mail_data["in_reply_to"] = line[12:].strip()

        if mail_data.get("mail_id"):
            try:
                mails.append(
                    MailSummaryItem(
                        mail_id=mail_data.get("mail_id", ""),
                        timestamp=mail_data.get("timestamp", ""),
                        from_email=mail_data.get("from_email", ""),
                        to=mail_data.get("to", []),
                        subject=mail_data.get("subject", ""),
                        thread_id=mail_data.get("thread_id"),
                        in_reply_to=mail_data.get("in_reply_to"),
                    )
                )
            except Exception:
                continue

    count = len(mails)
    has_more = count == limit

    return MailListResult(
        mails=mails,
        count=count,
        page=page,
        limit=limit,
        has_more=has_more,
    )


def _parse_mail_details_result(result_str: str) -> dict[str, Any] | None:
    """Parse the string result from read_mail into a dict."""
    error_prefixes = (
        "Error:",
        "Mail not found",
        "Mailbox is currently busy",
        "Invalid ",
    )
    if result_str.startswith(error_prefixes):
        return None

    mail_data: dict[str, Any] = {}
    lines = result_str.split("\n")

    in_body = False
    body_lines: list[str] = []
    in_attachments = False
    attachments: list[str] = []

    for i, line in enumerate(lines):
        if in_attachments:
            if line.startswith("  - "):
                attachments.append(line[4:])
            continue

        if in_body:
            # Only treat "Attachments:" as a section marker if the next line
            # starts with "  - " (actual attachment format), to avoid truncating
            # body content that happens to contain this exact text
            if line == "Attachments:":
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                if next_line.startswith("  - "):
                    in_attachments = True
                    continue
            body_lines.append(line)
            continue

        if line.startswith("Mail ID:"):
            mail_data["mail_id"] = line[8:].strip()
        elif line.startswith("Timestamp:"):
            mail_data["timestamp"] = line[10:].strip()
        elif line.startswith("From:"):
            mail_data["from_email"] = line[5:].strip()
        elif line.startswith("To:"):
            mail_data["to"] = [e.strip() for e in line[3:].strip().split(",")]
        elif line.startswith("CC:"):
            mail_data["cc"] = [e.strip() for e in line[3:].strip().split(",")]
        elif line.startswith("BCC:"):
            mail_data["bcc"] = [e.strip() for e in line[4:].strip().split(",")]
        elif line.startswith("Subject:"):
            mail_data["subject"] = line[8:].strip()
        elif line.startswith("Body Format:"):
            mail_data["body_format"] = line[12:].strip()
        elif line.startswith("Thread ID:"):
            mail_data["thread_id"] = line[10:].strip()
        elif line.startswith("In Reply To:"):
            mail_data["in_reply_to"] = line[12:].strip()
        elif line == "Body:":
            in_body = True

    if body_lines:
        mail_data["body"] = "\n".join(body_lines)

    if attachments:
        mail_data["attachments"] = attachments

    return mail_data if mail_data.get("mail_id") else None


def _is_mail_error(result: str) -> bool:
    """Check if mail operation result indicates an error."""
    error_prefixes = (
        "Invalid ",
        "Error:",
        "Error ",
        "Failed to",
        "Mail not found",
        "Mail data validation failed",
        "Mbox file not found",
        "Original mail not found",
        "Cannot ",
        "Mailbox is currently busy",
        "Validation failed",
    )
    return result.startswith(error_prefixes)


# ============ Meta-Tool Implementation ============
async def mail(request: MailInput) -> MailOutput:
    """Mail operations: send, read, list, search, reply, reply_all, and forward emails."""
    match request.action:
        case "help":
            return MailOutput(action="help", error=None, help=MAIL_HELP)

        case "send":
            if not request.to_email:
                return MailOutput(action="send", error="Required: to_email")
            if not request.subject:
                return MailOutput(action="send", error="Required: subject")
            if request.body is None:
                return MailOutput(action="send", error="Required: body")

            try:
                result = await _send_mail(
                    from_email=request.from_email or "user@example.com",
                    to_email=request.to_email,
                    subject=request.subject,
                    body=request.body,
                    cc=request.cc,
                    bcc=request.bcc,
                    attachments=request.attachments,
                    body_format=request.body_format or "plain",
                    thread_id=request.thread_id,
                    in_reply_to=request.in_reply_to,
                    references=request.references,
                )
                send_result = _parse_send_result(result)
                if not send_result.success:
                    return MailOutput(
                        action="send", error=send_result.error or send_result.message
                    )
                return MailOutput(action="send", error=None, send=send_result)
            except Exception as exc:
                return MailOutput(action="send", error=str(exc))

        case "read":
            if not request.mail_id:
                return MailOutput(action="read", error="Required: mail_id")

            try:
                result = await _read_mail(mail_id=request.mail_id)

                if _is_mail_error(result):
                    return MailOutput(action="read", error=result)

                mail_data = _parse_mail_details_result(result)
                if mail_data is None:
                    return MailOutput(action="read", error=result)

                return MailOutput(
                    action="read",
                    error=None,
                    read=MailDetailsResult(mail=mail_data),
                )
            except Exception as exc:
                return MailOutput(action="read", error=str(exc))

        case "list":
            try:
                limit = request.limit or DEFAULT_LIST_LIMIT

                if request.page is not None:
                    offset = request.page * limit
                    page = request.page
                else:
                    offset = request.offset or 0
                    page = offset // limit if offset else 0

                result = await _list_mails(limit=limit, offset=offset)

                if _is_mail_error(result):
                    return MailOutput(action="list", error=result)

                list_result = _parse_mail_list_result(result, limit, page)
                return MailOutput(action="list", error=None, list=list_result)
            except Exception as exc:
                return MailOutput(action="list", error=str(exc))

        case "search":
            try:
                limit = request.limit or DEFAULT_LIST_LIMIT
                result = await _search_mail(
                    from_email=request.search_from,
                    to_email=request.search_to,
                    subject=request.search_subject,
                    after_date=request.after_date,
                    before_date=request.before_date,
                    thread_id=request.search_thread_id,
                    limit=limit,
                )

                if _is_mail_error(result):
                    return MailOutput(action="search", error=result)

                search_result = _parse_mail_list_result(result, limit)
                return MailOutput(action="search", error=None, search=search_result)
            except Exception as exc:
                return MailOutput(action="search", error=str(exc))

        case "reply":
            if not request.mail_id:
                return MailOutput(action="reply", error="Required: mail_id")
            if request.body is None:
                return MailOutput(action="reply", error="Required: body")

            try:
                result = await _reply_mail(
                    original_mail_id=request.mail_id,
                    body=request.body,
                    attachments=request.attachments,
                    body_format=request.body_format or "plain",
                )

                if _is_mail_error(result):
                    return MailOutput(action="reply", error=result)

                reply_result = _parse_send_result(result)
                if not reply_result.success:
                    return MailOutput(
                        action="reply",
                        error=reply_result.error or reply_result.message,
                    )
                return MailOutput(action="reply", error=None, reply=reply_result)
            except Exception as exc:
                return MailOutput(action="reply", error=str(exc))

        case "reply_all":
            if not request.mail_id:
                return MailOutput(action="reply_all", error="Required: mail_id")
            if request.body is None:
                return MailOutput(action="reply_all", error="Required: body")

            try:
                result = await _reply_all_mail(
                    original_mail_id=request.mail_id,
                    body=request.body,
                    attachments=request.attachments,
                    body_format=request.body_format or "plain",
                )

                if _is_mail_error(result):
                    return MailOutput(action="reply_all", error=result)

                reply_all_result = _parse_send_result(result)
                if not reply_all_result.success:
                    return MailOutput(
                        action="reply_all",
                        error=reply_all_result.error or reply_all_result.message,
                    )
                return MailOutput(
                    action="reply_all", error=None, reply_all=reply_all_result
                )
            except Exception as exc:
                return MailOutput(action="reply_all", error=str(exc))

        case "forward":
            if not request.mail_id:
                return MailOutput(action="forward", error="Required: mail_id")
            if not request.to_email:
                return MailOutput(action="forward", error="Required: to_email")

            try:
                result = await _forward_mail(
                    original_mail_id=request.mail_id,
                    to_email=request.to_email,
                    body=request.body,
                    cc=request.cc,
                    bcc=request.bcc,
                    attachments=request.attachments,
                    body_format=request.body_format or "plain",
                )

                if _is_mail_error(result):
                    return MailOutput(action="forward", error=result)

                forward_result = _parse_send_result(result)
                if not forward_result.success:
                    return MailOutput(
                        action="forward",
                        error=forward_result.error or forward_result.message,
                    )
                return MailOutput(action="forward", error=None, forward=forward_result)
            except Exception as exc:
                return MailOutput(action="forward", error=str(exc))

        case _:
            return MailOutput(
                action=request.action, error=f"Unknown action: {request.action}"
            )


# ============ Schema Tool ============
class SchemaInput(BaseModel):
    """Input for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(
        ...,
        description="Model name: 'input', 'output', or a result type like 'SendResult', 'MailListResult'",
    )


class SchemaOutput(BaseModel):
    """Output for schema introspection."""

    model_config = ConfigDict(extra="forbid")
    model: str
    json_schema: dict[str, Any]


SCHEMAS: dict[str, type[BaseModel]] = {
    "input": MailInput,
    "output": MailOutput,
    "SendResult": SendResult,
    "MailDetailsResult": MailDetailsResult,
    "MailListResult": MailListResult,
    "MailSummaryItem": MailSummaryItem,
}


def mail_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for mail input/output models."""
    if request.model not in SCHEMAS:
        available = ", ".join(sorted(SCHEMAS.keys()))
        return SchemaOutput(
            model=request.model,
            json_schema={"error": f"Unknown model. Available: {available}"},
        )
    return SchemaOutput(
        model=request.model,
        json_schema=SCHEMAS[request.model].model_json_schema(),
    )

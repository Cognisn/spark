"""Email tools — send and draft emails via SMTP."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "send_email",
        "description": (
            "Send an email via SMTP. Supports HTML and plain text, multiple "
            "recipients (to, cc, bcc), and file attachments from allowed paths. "
            "This is a mutation tool — it will always require explicit user "
            "approval unless running in an autonomous action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of recipient email addresses.",
                },
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Email body content."},
                "body_type": {
                    "type": "string",
                    "enum": ["plain", "html"],
                    "description": "Body content type. Default: plain.",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC recipient email addresses.",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC recipient email addresses.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File paths to attach (must be within allowed paths).",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "draft_email",
        "description": (
            "Compose an email and save it as a .eml file in the specified "
            "directory instead of sending it.  Useful for preparing emails for "
            "review before sending."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of recipient email addresses.",
                },
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Email body content."},
                "body_type": {
                    "type": "string",
                    "enum": ["plain", "html"],
                    "description": "Body content type. Default: plain.",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC recipient email addresses.",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC recipient email addresses.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File paths to attach (must be within allowed paths).",
                },
                "save_path": {
                    "type": "string",
                    "description": "Directory to save the .eml file. Must be within allowed paths.",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
]


def get_tools() -> list[dict[str, Any]]:
    """Return email tool definitions."""
    return list(TOOLS)


def execute(
    tool_name: str,
    tool_input: dict[str, Any],
    config: dict[str, Any],
) -> str:
    """Execute an email tool."""
    email_config = config.get("embedded_tools", {}).get("email", {})

    if tool_name == "send_email":
        return _send_email(tool_input, email_config, config)
    elif tool_name == "draft_email":
        return _draft_email(tool_input, email_config, config)
    return f"Unknown email tool: {tool_name}"


def _build_message(
    tool_input: dict[str, Any],
    email_config: dict[str, Any],
    allowed_paths: list[str] | None = None,
    max_attachment_mb: float = 25,
) -> tuple[MIMEMultipart, str | None]:
    """Build a MIME message. Returns (message, error_string_or_none)."""
    to_addrs = tool_input.get("to", [])
    subject = tool_input.get("subject", "")
    body = tool_input.get("body", "")
    body_type = tool_input.get("body_type", "plain")
    cc_addrs = tool_input.get("cc", [])
    bcc_addrs = tool_input.get("bcc", [])
    attachments = tool_input.get("attachments", [])
    sender = email_config.get("sender", "")

    if not to_addrs:
        return MIMEMultipart(), "Error: at least one recipient is required."
    if not subject:
        return MIMEMultipart(), "Error: subject is required."
    if not body:
        return MIMEMultipart(), "Error: body is required."
    if not sender:
        return MIMEMultipart(), "Error: sender address is not configured in email settings."

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)

    msg.attach(MIMEText(body, body_type))

    # Attachments
    for file_path_str in attachments:
        file_path = Path(file_path_str).resolve()

        # Validate against allowed paths
        if allowed_paths:
            if not any(
                str(file_path).startswith(str(Path(ap).resolve()))
                for ap in allowed_paths
            ):
                return msg, f"Error: attachment '{file_path}' is outside allowed paths."

        if not file_path.is_file():
            return msg, f"Error: attachment '{file_path}' does not exist."

        # Check size
        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > max_attachment_mb:
            return msg, (
                f"Error: attachment '{file_path.name}' is {size_mb:.1f} MB, "
                f"exceeding the {max_attachment_mb} MB limit."
            )

        data = file_path.read_bytes()
        part = MIMEApplication(data, Name=file_path.name)
        part["Content-Disposition"] = f'attachment; filename="{file_path.name}"'
        msg.attach(part)

    return msg, None


def _send_email(
    tool_input: dict[str, Any],
    email_config: dict[str, Any],
    config: dict[str, Any],
) -> str:
    """Send an email via SMTP."""
    # Validate SMTP configuration
    host = email_config.get("host", "")
    if not host:
        return "Error: SMTP host is not configured. Go to Settings → Email to configure."

    port = int(email_config.get("port", 587))
    username = email_config.get("username", "")
    password = email_config.get("password", "")
    use_tls = email_config.get("use_tls", True)
    max_attachment_mb = float(email_config.get("max_attachment_mb", 25))

    # Get allowed paths for attachment validation
    fs_config = config.get("embedded_tools", {}).get("filesystem", {})
    allowed_paths = fs_config.get("allowed_paths", [])
    if isinstance(allowed_paths, str):
        allowed_paths = [p.strip() for p in allowed_paths.split(",") if p.strip()]

    msg, error = _build_message(tool_input, email_config, allowed_paths, max_attachment_mb)
    if error:
        return error

    to_addrs = tool_input.get("to", [])
    cc_addrs = tool_input.get("cc", [])
    bcc_addrs = tool_input.get("bcc", [])
    all_recipients = to_addrs + cc_addrs + bcc_addrs

    try:
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=context)
                if username and password:
                    server.login(username, password)
                server.sendmail(email_config.get("sender", ""), all_recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                if username and password:
                    server.login(username, password)
                server.sendmail(email_config.get("sender", ""), all_recipients, msg.as_string())

        recipient_summary = ", ".join(to_addrs)
        if cc_addrs:
            recipient_summary += f" (CC: {', '.join(cc_addrs)})"
        attachment_count = len(tool_input.get("attachments", []))
        attach_info = f" with {attachment_count} attachment(s)" if attachment_count else ""

        logger.info("Email sent to %s: %s", recipient_summary, tool_input.get("subject", ""))
        return (
            f"Email sent successfully to {recipient_summary}{attach_info}.\n"
            f"Subject: {tool_input.get('subject', '')}"
        )

    except smtplib.SMTPAuthenticationError:
        return "Error: SMTP authentication failed. Check username and password in settings."
    except smtplib.SMTPRecipientsRefused as e:
        return f"Error: recipients refused — {e}"
    except smtplib.SMTPException as e:
        return f"Error sending email: {e}"
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return f"Error: {e}"


def _draft_email(
    tool_input: dict[str, Any],
    email_config: dict[str, Any],
    config: dict[str, Any],
) -> str:
    """Save an email draft as a .eml file."""
    fs_config = config.get("embedded_tools", {}).get("filesystem", {})
    allowed_paths = fs_config.get("allowed_paths", [])
    if isinstance(allowed_paths, str):
        allowed_paths = [p.strip() for p in allowed_paths.split(",") if p.strip()]

    max_attachment_mb = float(email_config.get("max_attachment_mb", 25))
    msg, error = _build_message(tool_input, email_config, allowed_paths, max_attachment_mb)
    if error:
        return error

    # Determine save location
    save_path_str = tool_input.get("save_path", "")
    if save_path_str:
        save_dir = Path(save_path_str).resolve()
    elif allowed_paths:
        save_dir = Path(allowed_paths[0]).resolve()
    else:
        save_dir = Path.cwd().resolve()

    # Validate save path against allowed paths
    if allowed_paths:
        if not any(
            str(save_dir).startswith(str(Path(ap).resolve()))
            for ap in allowed_paths
        ):
            return f"Error: save path '{save_dir}' is outside allowed paths."

    if not save_dir.is_dir():
        return f"Error: save directory '{save_dir}' does not exist."

    # Generate filename from subject
    import re
    from datetime import datetime, timezone

    subject = tool_input.get("subject", "draft")
    safe_subject = re.sub(r"[^\w\s-]", "", subject)[:50].strip().replace(" ", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"draft_{safe_subject}_{timestamp}.eml"
    filepath = save_dir / filename

    filepath.write_text(msg.as_string(), encoding="utf-8")
    logger.info("Email draft saved to %s", filepath)
    return f"Email draft saved to: {filepath}"

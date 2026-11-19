"""
Yandex Mail MCP Server

Provides email tools for Claude Desktop via MCP protocol.
Uses IMAP for reading and SMTP for sending.
"""

import imaplib
import smtplib
import email
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import sys
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from imapclient import imap_utf7

VERSION = "0.0.1"

# Load environment variables from script's directory
SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(SCRIPT_DIR / ".env")

# Configure logging (not print - stdout is for MCP protocol)
logging.basicConfig(level=logging.INFO, filename=str(SCRIPT_DIR / "yandex_mail_mcp.log"))
logger = logging.getLogger(__name__)

# Yandex server settings
IMAP_SERVER = "imap.yandex.com"
IMAP_PORT = 993
SMTP_SERVER = "smtp.yandex.com"
SMTP_PORT = 587

# Credentials from environment
EMAIL = os.getenv("YANDEX_EMAIL")
PASSWORD = os.getenv("YANDEX_APP_PASSWORD")

# TLS context that verifies the server certificate chain and hostname.
# Without an explicit context, stdlib imaplib/smtplib default to
# ssl._create_stdlib_context() (CERT_NONE), letting any network-path attacker
# with a self-signed certificate capture the app password.
_TLS_CONTEXT = ssl.create_default_context()

# Create MCP server
mcp = FastMCP("Yandex Mail")


def decode_mime_header(header_value: str) -> str:
    """Decode MIME-encoded email header."""
    if not header_value:
        return ""
    decoded_parts = []
    for part, charset in decode_header(header_value):
        if isinstance(part, bytes):
            charset = charset or "utf-8"
            try:
                decoded_parts.append(part.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def decode_body(payload, charset: str) -> str:
    """Decode an email body with the same guarded fallback as decode_mime_header.

    Remote senders control the Content-Type charset parameter; an unknown codec
    name must never crash the tool call (audit finding: uncaught LookupError).
    """
    payload = payload or b""
    for cs in (charset or "utf-8", "utf-8"):
        try:
            return payload.decode(cs, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


@contextmanager
def imap_connection():
    """Context manager for IMAP connection."""
    if not EMAIL or not PASSWORD:
        raise ValueError("YANDEX_EMAIL and YANDEX_APP_PASSWORD must be set in .env")

    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, ssl_context=_TLS_CONTEXT)
    try:
        conn.login(EMAIL, PASSWORD)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


@contextmanager
def smtp_connection():
    """Context manager for SMTP connection."""
    if not EMAIL or not PASSWORD:
        raise ValueError("YANDEX_EMAIL and YANDEX_APP_PASSWORD must be set in .env")

    conn = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
    try:
        conn.starttls(context=_TLS_CONTEXT)
        conn.login(EMAIL, PASSWORD)
        yield conn
    finally:
        try:
            conn.quit()
        except Exception:
            pass


def decode_folder_name(imap_name: str) -> str:
    """Decode IMAP modified UTF-7 folder name to readable string."""
    try:
        return imap_utf7.decode(imap_name.encode())
    except Exception:
        return imap_name


@mcp.tool()
def list_folders() -> list[dict]:
    """
    List all mail folders in the Yandex mailbox.

    Returns list of folders with:
    - name: Human-readable folder name (decoded from IMAP UTF-7)
    - imap_name: Raw IMAP folder name (use this for other operations like search_emails)
    """
    with imap_connection() as conn:
        status, folder_data = conn.list()
        if status != "OK":
            raise Exception("Failed to list folders")

        folders = []
        for item in folder_data:
            if isinstance(item, bytes):
                # Parse folder info: (\\Attr1 \\Attr2) "/" "FolderName"
                decoded = item.decode("utf-8", errors="replace")
                # Extract folder name (after last quote pair)
                parts = decoded.rsplit('"', 2)
                if len(parts) >= 2:
                    imap_name = parts[-2]
                    human_name = decode_folder_name(imap_name)
                    folders.append({
                        "name": human_name,
                        "imap_name": imap_name
                    })

        return folders


def build_imap_search_criteria(query: str) -> list[str]:
    """
    Parse user-friendly query into IMAP search criteria with proper quoting.

    Handles: FROM, TO, CC, BCC, SUBJECT, BODY, TEXT
    These keywords need their values quoted for IMAP.
    """
    if not query or query.upper() == "ALL":
        return ["ALL"]

    # Keywords that need their following value quoted
    keywords_needing_quotes = {"FROM", "TO", "CC", "BCC", "SUBJECT", "BODY", "TEXT"}

    result = []
    tokens = query.split()
    i = 0

    while i < len(tokens):
        token = tokens[i]
        upper_token = token.upper()

        if upper_token in keywords_needing_quotes and i + 1 < len(tokens):
            # This keyword needs the next value quoted
            value = tokens[i + 1]
            # Remove existing quotes if any, then add proper quotes
            value = value.strip('"\'')
            result.append(upper_token)
            result.append(f'"{value}"')
            i += 2
        else:
            result.append(token)
            i += 1

    return result


@mcp.tool()
def search_emails(
    folder: str = "INBOX",
    query: str = "ALL",
    limit: int = 20
) -> list[dict]:
    """
    Search emails in a folder.

    Args:
        folder: Mailbox folder (default: INBOX). Use list_folders() to see available folders.
        query: IMAP search query. Examples:
            - "ALL" - all emails
            - "UNSEEN" - unread emails
            - "FROM sender@example.com" - from specific sender
            - "SUBJECT hello" - subject contains "hello"
            - "SINCE 01-Dec-2024" - emails since date
            - "BEFORE 31-Dec-2024" - emails before date
            - Can combine: "UNSEEN FROM boss@company.com"
        limit: Maximum number of emails to return (default: 20)

    Returns list of email summaries with id, subject, from, date.
    """
    with imap_connection() as conn:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Search emails with properly quoted criteria
        criteria = build_imap_search_criteria(query)

        # Use UTF-8 charset for non-ASCII queries (Cyrillic, etc.)
        has_non_ascii = any(ord(c) > 127 for c in query)
        if has_non_ascii:
            # For UTF-8 search, we need to pass criteria as a single string
            criteria_str = " ".join(criteria)
            status, message_ids = conn.search("UTF-8", criteria_str.encode("utf-8"))
        else:
            status, message_ids = conn.search(None, *criteria)

        if status != "OK":
            raise Exception(f"Search failed: {query}")

        ids = message_ids[0].split()
        # Get most recent emails (last N)
        ids = ids[-limit:] if len(ids) > limit else ids
        ids = list(reversed(ids))  # Most recent first

        emails = []
        for msg_id in ids:
            # Fetch headers only for performance
            status, msg_data = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if status != "OK":
                continue

            raw_header = msg_data[0][1]
            msg = email.message_from_bytes(raw_header)

            subject = decode_mime_header(msg.get("Subject", ""))
            from_addr = decode_mime_header(msg.get("From", ""))
            date_str = msg.get("Date", "")

            emails.append({
                "id": msg_id.decode("utf-8"),
                "subject": subject,
                "from": from_addr,
                "date": date_str
            })

        return emails


@mcp.tool()
def read_email(folder: str, email_id: str) -> dict:
    """
    Read full email content by ID.

    Args:
        folder: Mailbox folder containing the email
        email_id: Email ID from search_emails() result

    Returns email with subject, from, to, date, body_text, body_html, attachments list.
    """
    with imap_connection() as conn:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.fetch(email_id.encode(), "(RFC822)")
        if status != "OK":
            raise Exception(f"Failed to fetch email: {email_id}")

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_header(msg.get("Subject", ""))
        from_addr = decode_mime_header(msg.get("From", ""))
        to_addr = decode_mime_header(msg.get("To", ""))
        date_str = msg.get("Date", "")

        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        attachments.append({
                            "filename": decode_mime_header(filename),
                            "content_type": content_type,
                            "size": len(part.get_payload(decode=True) or b"")
                        })
                elif content_type == "text/plain" and not body_text:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    body_text = decode_body(payload, charset)
                elif content_type == "text/html" and not body_html:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    body_html = decode_body(payload, charset)
        else:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            if msg.get_content_type() == "text/html":
                body_html = decode_body(payload, charset)
            else:
                body_text = decode_body(payload, charset)

        return {
            "id": email_id,
            "subject": subject,
            "from": from_addr,
            "to": to_addr,
            "date": date_str,
            "body_text": body_text,
            "body_html": body_html,
            "attachments": attachments
        }


@mcp.tool()
def download_attachment(
    folder: str,
    email_id: str,
    filename: str,
    save_dir: Optional[str] = None
) -> dict:
    """
    Download an email attachment to disk.

    Args:
        folder: Mailbox folder containing the email
        email_id: Email ID from search_emails() result
        filename: Attachment filename to download (from read_email attachments list)
        save_dir: Directory to save the file (default: ~/Downloads)

    Returns dict with saved file path and size.
    """
    # Default save directory
    if save_dir is None:
        save_dir = str(Path.home() / "Downloads")

    save_path = Path(save_dir)
    if not save_path.exists():
        save_path.mkdir(parents=True, exist_ok=True)

    with imap_connection() as conn:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.fetch(email_id.encode(), "(RFC822)")
        if status != "OK":
            raise Exception(f"Failed to fetch email: {email_id}")

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        # Find the attachment
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" not in content_disposition:
                continue

            part_filename = part.get_filename()
            if part_filename:
                decoded_filename = decode_mime_header(part_filename)
                if decoded_filename == filename:
                    # Found the attachment
                    payload = part.get_payload(decode=True)
                    if payload:
                        # Confine the write to the save directory: reduce the
                        # (remote-controlled) attachment name to a safe basename,
                        # resolve the final path under save_dir, and never follow
                        # symlinks (audit finding: path traversal / overwrite).
                        base = os.path.basename(decoded_filename.replace("\\", "/"))
                        if not base or base in (".", "..") or base.startswith("."):
                            raise ValueError(f"Unsafe attachment filename: {decoded_filename!r}")
                        root = save_path.resolve()
                        file_path = (root / base).resolve()
                        if os.path.commonpath([str(root), str(file_path)]) != str(root):
                            raise ValueError("Resolved path escapes save_dir")
                        fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "wb") as f:
                            f.write(payload)

                        return {
                            "status": "downloaded",
                            "filename": decoded_filename,
                            "path": str(file_path),
                            "size": len(payload),
                            "content_type": part.get_content_type()
                        }

        raise Exception(f"Attachment not found: {filename}")


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False
) -> dict:
    """
    Send an email via Yandex SMTP.

    Args:
        to: Recipient email address (comma-separated for multiple)
        subject: Email subject
        body: Email body (plain text or HTML based on html flag)
        cc: CC recipients (optional, comma-separated)
        bcc: BCC recipients (optional, comma-separated)
        html: If True, body is treated as HTML (default: False)

    Returns confirmation with message ID.
    """
    if not EMAIL:
        raise ValueError("YANDEX_EMAIL must be set in .env")

    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = to
    if cc:
        msg["Cc"] = cc

    # Build recipient list
    recipients = [addr.strip() for addr in to.split(",")]
    if cc:
        recipients.extend([addr.strip() for addr in cc.split(",")])
    if bcc:
        recipients.extend([addr.strip() for addr in bcc.split(",")])

    with smtp_connection() as conn:
        conn.send_message(msg, EMAIL, recipients)

    return {
        "status": "sent",
        "to": to,
        "subject": subject,
        "cc": cc,
        "bcc": bcc
    }


@mcp.tool()
def move_email(folder: str, email_id: str, destination: str) -> dict:
    """
    Move an email to another folder.

    Args:
        folder: Source folder containing the email
        email_id: Email ID to move
        destination: Destination folder name

    Returns confirmation of move.
    """
    with imap_connection() as conn:
        status, _ = conn.select(folder)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Copy to destination
        status, _ = conn.copy(email_id.encode(), destination)
        if status != "OK":
            raise Exception(f"Failed to copy email to: {destination}")

        # Mark original as deleted
        status, _ = conn.store(email_id.encode(), "+FLAGS", "\\Deleted")
        if status != "OK":
            raise Exception("Failed to mark original as deleted")

        # IMAP EXPUNGE is folder-wide: only expunge when no OTHER messages are
        # pending deletion (they would be swept as a side effect).
        status, deleted = conn.search(None, "DELETED")
        flagged = deleted[0].split() if status == "OK" and deleted and deleted[0] else []
        if flagged != [email_id.encode()]:
            return {
                "status": "moved_copy_created_flag_not_expunged",
                "email_id": email_id,
                "from_folder": folder,
                "to_folder": destination
            }
        conn.expunge()

        return {
            "status": "moved",
            "email_id": email_id,
            "from_folder": folder,
            "to_folder": destination
        }


@mcp.tool()
def delete_email(folder: str, email_id: str) -> dict:
    """
    Delete an email (move to Trash).

    Args:
        folder: Folder containing the email
        email_id: Email ID to delete

    Returns confirmation of deletion.
    """
    # Yandex uses "Trash" folder (may also be localized)
    trash_folder = "Trash"

    with imap_connection() as conn:
        status, _ = conn.select(folder)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Try to move to Trash
        status, _ = conn.copy(email_id.encode(), trash_folder)
        if status != "OK":
            # Failing to reach Trash must never escalate to permanent deletion:
            # fail safely and leave the message untouched.
            raise Exception(f"Could not move email to {trash_folder}; email left in place")

        # Mark original as deleted
        conn.store(email_id.encode(), "+FLAGS", "\\Deleted")

        # IMAP EXPUNGE is folder-wide: only expunge when no OTHER messages are
        # pending deletion (they would be swept as a side effect).
        status, deleted = conn.search(None, "DELETED")
        flagged = deleted[0].split() if status == "OK" and deleted and deleted[0] else []
        if flagged != [email_id.encode()]:
            return {
                "status": "moved_to_trash_flag_not_expunged",
                "email_id": email_id,
                "folder": folder
            }
        conn.expunge()

        return {
            "status": "moved_to_trash",
            "email_id": email_id,
            "folder": folder
        }


if __name__ == "__main__":
    mcp.run(transport="stdio")

"""
Yandex Mail MCP Server

Provides email tools for Claude Desktop via MCP protocol.
Uses IMAP for reading and SMTP for sending.
"""

import imaplib
import smtplib
import email
import re
import socket
import time
from email import encoders
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.header import decode_header
from email.utils import parsedate_to_datetime, getaddresses, formataddr
import os
import sys
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from imapclient import imap_utf7

VERSION = "0.1.2"

SCRIPT_DIR = Path(__file__).parent.resolve()


def _resolve_dotenv_path() -> Optional[Path]:
    """
    Find a .env file in a priority order that works for both
    direct-invocation and uvx / site-packages installs:

    1. $YANDEX_MAIL_MCP_ENV — explicit override
    2. $PWD/.env — project-local
    3. $XDG_CONFIG_HOME/yandex-mail-mcp/.env (or ~/.config/...)
    4. SCRIPT_DIR/.env — original behavior, only meaningful for source checkouts
    """
    override = os.environ.get("YANDEX_MAIL_MCP_ENV")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p

    candidates = [
        Path.cwd() / ".env",
        Path(
            os.environ.get(
                "XDG_CONFIG_HOME", str(Path.home() / ".config")
            )
        ) / "yandex-mail-mcp" / ".env",
        SCRIPT_DIR / ".env",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


_dotenv_path = _resolve_dotenv_path()
if _dotenv_path is not None:
    # load_dotenv does NOT override existing env vars by default, so values
    # set in the MCP client config (e.g. Claude Desktop's `env` key) take
    # precedence over anything in a .env file.
    load_dotenv(_dotenv_path)


def _resolve_log_file() -> Path:
    """
    Choose a log file path that works for both direct-invocation and
    uvx / site-packages installs.

    1. $YANDEX_MAIL_MCP_LOG_FILE — explicit override
    2. $XDG_STATE_HOME/yandex-mail-mcp/yandex_mail_mcp.log
       (or ~/.local/state/yandex-mail-mcp/...)
    3. SCRIPT_DIR/yandex_mail_mcp.log — if the directory is writable
       (source checkout scenario)
    4. $TMPDIR/yandex_mail_mcp.log — last-resort fallback
    """
    override = os.environ.get("YANDEX_MAIL_MCP_LOG_FILE")
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    state_home = Path(
        os.environ.get(
            "XDG_STATE_HOME", str(Path.home() / ".local" / "state")
        )
    )
    user_log_dir = state_home / "yandex-mail-mcp"
    try:
        user_log_dir.mkdir(parents=True, exist_ok=True)
        return user_log_dir / "yandex_mail_mcp.log"
    except OSError:
        pass

    # Source checkout fallback: write next to server.py if possible
    try:
        test_path = SCRIPT_DIR / ".write-test"
        test_path.touch()
        test_path.unlink()
        return SCRIPT_DIR / "yandex_mail_mcp.log"
    except OSError:
        pass

    return Path(os.environ.get("TMPDIR", "/tmp")) / "yandex_mail_mcp.log"


# Configure logging (not print - stdout is for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    filename=str(_resolve_log_file()),
)
logger = logging.getLogger(__name__)

# Yandex server settings
IMAP_SERVER = "imap.yandex.com"
IMAP_PORT = 993
SMTP_SERVER = "smtp.yandex.com"
SMTP_PORT = 587

# Credentials from environment
EMAIL = os.getenv("YANDEX_EMAIL")
PASSWORD = os.getenv("YANDEX_APP_PASSWORD")

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


# Transient network errors that are worth retrying a couple of times before
# giving up. gaierror especially can fire briefly on flaky DNS (WSL2, corporate
# proxies, home WiFi under load). `time.sleep` backoff is short so end-users
# don't notice it.
_TRANSIENT_NET_ERRORS = (
    socket.gaierror,
    socket.timeout,
    TimeoutError,
    ConnectionError,
    OSError,
)


def _connect_with_retry(factory, attempts: int = 3, backoff: float = 0.5):
    """
    Call `factory()` up to `attempts` times, retrying on transient network
    errors with a short linear backoff. Re-raises the last error on failure.
    """
    last_err: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return factory()
        except _TRANSIENT_NET_ERRORS as e:
            last_err = e
            if i < attempts - 1:
                logger.warning(
                    "Transient network error on attempt %d/%d: %s",
                    i + 1, attempts, e,
                )
                time.sleep(backoff * (i + 1))
    assert last_err is not None  # for type checker
    raise last_err


@contextmanager
def imap_connection():
    """Context manager for IMAP connection (with transient-error retry)."""
    if not EMAIL or not PASSWORD:
        raise ValueError("YANDEX_EMAIL and YANDEX_APP_PASSWORD must be set in .env")

    conn = _connect_with_retry(lambda: imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT))
    try:
        conn.login(EMAIL, PASSWORD)
        # Explicitly refresh capabilities after auth — some servers only
        # advertise extensions like MOVE, SPECIAL-USE or UIDPLUS after
        # successful LOGIN, and don't include them in the initial greeting
        # or the LOGIN OK response code.
        try:
            conn.capability()
        except Exception:
            pass
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


@contextmanager
def smtp_connection():
    """Context manager for SMTP connection (with transient-error retry)."""
    if not EMAIL or not PASSWORD:
        raise ValueError("YANDEX_EMAIL and YANDEX_APP_PASSWORD must be set in .env")

    conn = _connect_with_retry(lambda: smtplib.SMTP(SMTP_SERVER, SMTP_PORT))
    try:
        conn.starttls()
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


def encode_folder_name(name: str) -> str:
    """
    Encode folder name to IMAP modified UTF-7 if it contains non-ASCII.

    Pass-through for ASCII input (including already-encoded UTF-7 like
    `&BCcENQRABDcENwQwBDw-`), so callers can safely pass either human-readable
    names ("Корзина") or raw imap_name values from list_folders().
    """
    if all(ord(c) < 128 for c in name):
        return name
    try:
        return imap_utf7.encode(name).decode("ascii")
    except Exception:
        return name


def _parse_folder_line(line) -> Optional[tuple[list[str], str]]:
    """
    Parse an IMAP LIST response line into (attrs, imap_name).

    Handles both quoted folder names (`"Trash"`) and unquoted atoms (`INBOX`),
    and both quoted and NIL hierarchy delimiters. Returns None if the line
    cannot be parsed.
    """
    if not isinstance(line, (bytes, bytearray)):
        return None
    decoded = line.decode("utf-8", errors="replace")

    # Attributes list: (\Attr1 \Attr2)
    if not decoded.startswith("("):
        return None
    attrs_end = decoded.find(")")
    if attrs_end < 0:
        return None
    attrs = decoded[1:attrs_end].split()

    rest = decoded[attrs_end + 1 :].strip()

    # Hierarchy delimiter: "/" or NIL
    if rest.startswith('"'):
        delim_end = rest.find('"', 1)
        if delim_end < 0:
            return None
        rest = rest[delim_end + 1 :].strip()
    elif rest[:3].upper() == "NIL":
        rest = rest[3:].strip()
    else:
        return None

    if not rest:
        return None

    # Mailbox name: quoted string or atom. Strip once more to be robust
    # against any trailing whitespace or CR the server may have left on.
    rest = rest.rstrip()

    if rest.startswith('"'):
        if len(rest) < 2 or not rest.endswith('"'):
            return None
        imap_name = rest[1:-1]
    else:
        imap_name = rest.split()[0]

    return attrs, imap_name


def _find_trash_folder(conn) -> str:
    """
    Find the Trash folder via IMAP SPECIAL-USE attribute with fallbacks.

    Preference order:
    1. Folder with \\Trash special-use attribute (RFC 6154)
    2. Folder whose decoded name matches Trash/Корзина/Deleted Items
    3. Literal "Trash" as last resort
    """
    try:
        status, folder_data = conn.list()
    except Exception:
        return "Trash"

    if status != "OK" or not folder_data:
        return "Trash"

    parsed: list[tuple[list[str], str]] = []
    for item in folder_data:
        result = _parse_folder_line(item)
        if result is not None:
            parsed.append(result)

    # Pass 1: \Trash special-use attribute
    for attrs, imap_name in parsed:
        if any(a.lower() == "\\trash" for a in attrs):
            return imap_name

    # Pass 2: known localized names
    known = {"trash", "корзина", "deleted items", "deleted messages"}
    for _attrs, imap_name in parsed:
        if decode_folder_name(imap_name).strip().casefold() in known:
            return imap_name

    return "Trash"


def _find_sent_folder(conn) -> Optional[str]:
    """
    Find the Sent folder via IMAP \\Sent SPECIAL-USE (RFC 6154) with fallbacks.
    Returns None if no suitable folder found.
    """
    try:
        status, folder_data = conn.list()
    except Exception:
        return None

    if status != "OK" or not folder_data:
        return None

    parsed: list[tuple[list[str], str]] = []
    for item in folder_data:
        result = _parse_folder_line(item)
        if result is not None:
            parsed.append(result)

    # Pass 1: \Sent special-use attribute
    for attrs, imap_name in parsed:
        if any(a.lower() == "\\sent" for a in attrs):
            return imap_name

    # Pass 2: known localized names
    known = {
        "sent", "sent items", "sent messages",
        "отправленные", "отправленные письма", "отправленная почта",
    }
    for _attrs, imap_name in parsed:
        if decode_folder_name(imap_name).strip().casefold() in known:
            return imap_name

    return None


def _has_capability(conn, cap: str) -> bool:
    """Check if the IMAP server advertises a capability (case-insensitive)."""
    caps = getattr(conn, "capabilities", None) or ()
    target = cap.upper()
    for c in caps:
        if isinstance(c, bytes):
            c = c.decode("ascii", errors="ignore")
        if c.upper() == target:
            return True
    return False


def _quote_folder_for_command(encoded: str) -> str:
    """
    Quote a UTF-7 encoded folder name for use as a raw IMAP command argument.

    imaplib's high-level methods (select, copy, rename, ...) handle quoting
    internally, but _simple_command passes tokens verbatim. Folder names
    containing spaces or other non-atom characters must be quoted explicitly
    so the server parses them as a single mailbox argument.
    """
    needs_quote = not encoded or any(
        c in encoded for c in ' \t"\\()[]{}%*'
    )
    if not needs_quote:
        return encoded
    escaped = encoded.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_message(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    html: bool = False,
    attachments: Optional[list[str]] = None,
    extra_headers: Optional[dict] = None,
):
    """
    Build a MIME message for SMTP sending. Shared between send_email and
    reply_email. Handles plain/HTML bodies, file attachments (with RFC 2231
    encoding for non-ASCII names), and arbitrary extra headers.

    Returns (msg, attached_names) tuple.
    """
    if not EMAIL:
        raise ValueError("YANDEX_EMAIL must be set in .env")

    body_subtype = "html" if html else "plain"
    attached_names: list[str] = []

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body, body_subtype, "utf-8"))
        for filepath in attachments:
            path = Path(filepath).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Attachment not found: {filepath}")
            logger.info("send_email attaching file: %s", path)
            with open(path, "rb") as f:
                data = f.read()
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            # Keyword form of add_header applies RFC 2231 encoding
            # automatically for non-ASCII filenames.
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=path.name,
            )
            msg.attach(part)
            attached_names.append(path.name)
    elif html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = to
    if cc:
        msg["Cc"] = cc

    if extra_headers:
        for name, value in extra_headers.items():
            if value:
                msg[name] = value

    return msg, attached_names


def _save_to_sent_folder(msg) -> Optional[str]:
    """
    Append a sent message to the Sent folder via IMAP APPEND.
    Returns the decoded folder name on success, None on any failure
    (including no Sent folder found — failure is non-fatal).
    """
    try:
        with imap_connection() as conn:
            sent = _find_sent_folder(conn)
            if not sent:
                logger.warning("save_to_sent: no Sent folder found")
                return None

            date_time = imaplib.Time2Internaldate(time.time())
            message_bytes = msg.as_bytes()

            status, _ = conn.append(sent, "\\Seen", date_time, message_bytes)
            if status != "OK":
                logger.warning("save_to_sent: APPEND failed with status %s", status)
                return None

            return decode_folder_name(sent)
    except Exception as e:
        logger.warning("save_to_sent: unexpected error: %s", e)
        return None


_RE_PREFIX_RE = re.compile(r"^(\s*re\s*:\s*)+", re.IGNORECASE)


def _dedupe_re_prefix(subject: str) -> str:
    """Strip any existing Re:/RE:/re: prefixes and add exactly one."""
    stripped = _RE_PREFIX_RE.sub("", subject or "").strip()
    return f"Re: {stripped}" if stripped else "Re:"


def _trim_references(refs: str, max_ids: int = 10, max_bytes: int = 998) -> str:
    """
    Trim a References header to stay under common length limits.
    Preserves the thread root (first ID) and most recent entries.
    """
    ids = (refs or "").split()
    if not ids:
        return ""
    if len(ids) > max_ids:
        ids = [ids[0]] + ids[-(max_ids - 1) :]
    result = " ".join(ids)
    while len(result.encode("utf-8", errors="replace")) > max_bytes and len(ids) > 2:
        ids.pop(1)  # drop second-oldest, keep root + recent
        result = " ".join(ids)
    return result


def _set_flags_impl(
    folder: str,
    email_id: str,
    add: Optional[list[str]] = None,
    remove: Optional[list[str]] = None,
) -> dict:
    """Internal implementation of flag manipulation via UID STORE."""
    if not add and not remove:
        raise ValueError("At least one of add/remove must be non-empty")

    # Guard against flag strings that would corrupt IMAP STORE syntax.
    # IMAP flags are either system flags (\Seen, \Flagged, ...) or keywords
    # (atoms per RFC 3501): no whitespace, parens, quotes, braces, percent,
    # asterisk, or non-printable characters.
    _bad_flag_chars = set(' \t\r\n"\\()[]{}%*')
    for flag in list(add or []) + list(remove or []):
        if not flag or not isinstance(flag, str):
            raise ValueError(f"Invalid flag: {flag!r}")
        # Allow leading backslash for system flags (\Seen etc.) but reject
        # backslash elsewhere or as part of the character set above.
        body = flag[1:] if flag.startswith("\\") else flag
        if not body or any(c in _bad_flag_chars for c in body):
            raise ValueError(
                f"Invalid flag {flag!r}: contains reserved IMAP characters"
            )

    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder))
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        if add:
            flag_list = " ".join(add)
            status, _ = conn.uid(
                "STORE", email_id, "+FLAGS", f"({flag_list})"
            )
            if status != "OK":
                raise Exception(f"Failed to add flags: {flag_list}")

        if remove:
            flag_list = " ".join(remove)
            status, _ = conn.uid(
                "STORE", email_id, "-FLAGS", f"({flag_list})"
            )
            if status != "OK":
                raise Exception(f"Failed to remove flags: {flag_list}")

        return {
            "status": "ok",
            "email_id": email_id,
            "folder": folder,
            "added": list(add or []),
            "removed": list(remove or []),
        }


# --- bulk operation helpers --------------------------------------------------

# Conservative batch size for multi-UID commands. IMAP command line length is
# implementation-defined but typically capped around 8 KB. With 6-7 digit UIDs
# and commas, 500 UIDs ≈ 4 KB of command — well under any reasonable limit.
_BULK_UID_CHUNK = 500


def _normalize_uid_list(email_ids: list[str]) -> list[str]:
    """Validate a list of UIDs and return them as a normalized list of strings.

    Per RFC 3501 §2.3.1.1, UIDs are non-zero 32-bit unsigned integers.
    """
    if not email_ids:
        raise ValueError("email_ids list is empty")
    out: list[str] = []
    for u in email_ids:
        s = str(u).strip()
        if not s.isdigit() or int(s) == 0:
            raise ValueError(
                f"Invalid UID (must be a positive integer, got {u!r})"
            )
        out.append(s)
    return out


def _chunk(seq: list[str], n: int):
    """Yield successive n-sized chunks from seq."""
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# --- BODYSTRUCTURE parser ----------------------------------------------------
#
# Parses the nested s-expression format from IMAP FETCH BODYSTRUCTURE responses.
# Tokens: '(', ')', quoted-string, NIL, number, atom.
#
# Basic (non-multipart) part fields per RFC 3501 §7.4.2:
#   type subtype params[=list|NIL] id description encoding size
#   (+ lines for text/*; + envelope+body+lines for message/rfc822)
#   md5 disposition language location
#
# Multipart: list of child parts + subtype + params + disposition + lang + loc


def _tokenize_bodystructure(data: bytes) -> list:
    """Tokenize an IMAP bodystructure byte string into a flat token list."""
    tokens: list = []
    i = 0
    n = len(data)
    while i < n:
        c = data[i : i + 1]
        if c in (b" ", b"\t", b"\r", b"\n"):
            i += 1
            continue
        if c == b"(":
            tokens.append("(")
            i += 1
            continue
        if c == b")":
            tokens.append(")")
            i += 1
            continue
        if c == b'"':
            # Quoted string
            j = i + 1
            buf = bytearray()
            while j < n:
                cj = data[j : j + 1]
                if cj == b"\\" and j + 1 < n:
                    buf.append(data[j + 1])
                    j += 2
                    continue
                if cj == b'"':
                    break
                buf.extend(cj)
                j += 1
            tokens.append(bytes(buf).decode("utf-8", errors="replace"))
            i = j + 1
            continue
        # Atom / NIL / number: read until whitespace or paren
        j = i
        while j < n and data[j : j + 1] not in (b" ", b"\t", b"\r", b"\n", b"(", b")"):
            j += 1
        raw = data[i:j].decode("ascii", errors="replace")
        if raw.upper() == "NIL":
            tokens.append(None)
        else:
            try:
                tokens.append(int(raw))
            except ValueError:
                tokens.append(raw)
        i = j
    return tokens


def _parse_bodystructure_list(tokens: list, pos: int):
    """
    Recursive-descent parser over the tokens produced by _tokenize_bodystructure.
    Returns (parsed_tree, next_pos). The parsed tree is the same nested list
    structure as the original s-expression, with child lists wrapped in Python
    lists and atoms/strings/numbers/None as leaves.
    """
    if pos >= len(tokens):
        return None, pos
    tok = tokens[pos]
    if tok != "(":
        return tok, pos + 1
    pos += 1
    result: list = []
    while pos < len(tokens) and tokens[pos] != ")":
        node, pos = _parse_bodystructure_list(tokens, pos)
        result.append(node)
    return result, pos + 1  # skip closing ')'


def _bodystructure_params_to_dict(params) -> dict:
    """Convert a parameter list ['key1', 'val1', 'key2', 'val2'] → dict."""
    if not isinstance(params, list):
        return {}
    out: dict = {}
    for i in range(0, len(params) - 1, 2):
        k = params[i]
        v = params[i + 1]
        if isinstance(k, str):
            out[k.lower()] = v
    return out


def _walk_bodystructure(tree, part_prefix: str = "") -> list[dict]:
    """
    Walk a parsed bodystructure tree and yield a flat list of parts with
    user-friendly metadata (part number, type, size, filename, etc.).

    Part numbering follows RFC 3501:
    - single-part message: "1"
    - multipart/mixed with text + attach: "1", "2"
    - nested multipart/alternative inside mixed: "1.1", "1.2", "2"
    """
    parts: list[dict] = []

    # Multipart: first element is a list (the first child).
    if isinstance(tree, list) and tree and isinstance(tree[0], list):
        # Children are at the start; walk until we hit a non-list (the subtype)
        child_idx = 0
        for child in tree:
            if not isinstance(child, list):
                break
            child_number = (
                f"{part_prefix}.{child_idx + 1}" if part_prefix else f"{child_idx + 1}"
            )
            parts.extend(_walk_bodystructure(child, child_number))
            child_idx += 1
        return parts

    # Single part:
    # ( type subtype params id desc encoding size [lines] md5 disposition ... )
    if not isinstance(tree, list) or len(tree) < 7:
        return parts

    type_ = tree[0] if isinstance(tree[0], str) else "application"
    subtype = tree[1] if isinstance(tree[1], str) else "octet-stream"
    params = _bodystructure_params_to_dict(tree[2])
    size = tree[6] if len(tree) > 6 and isinstance(tree[6], int) else None

    # Disposition is at different indices for text / message-rfc822 / other.
    # Per RFC 3501 §7.4.2, basic fields are at indices 0-6, then:
    # text/*         : 7=lines, 8=md5, 9=disposition
    # message/rfc822 : 7=envelope, 8=body, 9=lines, 10=md5, 11=disposition
    # everything else: 7=md5, 8=disposition
    type_lower = type_.lower()
    subtype_lower = subtype.lower() if isinstance(subtype, str) else ""
    is_text = type_lower == "text"
    is_message_rfc822 = type_lower == "message" and subtype_lower == "rfc822"
    if is_message_rfc822:
        disp_idx = 11
    elif is_text:
        disp_idx = 9
    else:
        disp_idx = 8

    filename = None
    disposition = None
    if len(tree) > disp_idx and isinstance(tree[disp_idx], list) and tree[disp_idx]:
        disp_body = tree[disp_idx]
        if disp_body and isinstance(disp_body[0], str):
            disposition = disp_body[0].lower()
        if len(disp_body) > 1:
            disp_params = _bodystructure_params_to_dict(disp_body[1])
            filename = disp_params.get("filename")

    if not filename:
        filename = params.get("name")

    part: dict = {
        "part": part_prefix or "1",
        "type": f"{type_}/{subtype}".lower(),
        "size": size,
    }
    if "charset" in params:
        part["charset"] = params["charset"]
    if disposition:
        part["disposition"] = disposition
    if filename:
        if isinstance(filename, bytes):
            filename = filename.decode("utf-8", errors="replace")
        part["filename"] = decode_mime_header(str(filename))

    parts.append(part)
    return parts


def parse_bodystructure(raw: bytes) -> list[dict]:
    """Public entrypoint: parse FETCH BODYSTRUCTURE response bytes → part list."""
    tokens = _tokenize_bodystructure(raw)
    tree, _ = _parse_bodystructure_list(tokens, 0)
    if tree is None:
        return []
    return _walk_bodystructure(tree)


# --- Fwd: prefix dedupe (symmetric to _dedupe_re_prefix) --------------------

_FWD_PREFIX_RE = re.compile(r"^(\s*(?:fwd?|fw)\s*:\s*)+", re.IGNORECASE)


def _dedupe_fwd_prefix(subject: str) -> str:
    """Strip any existing Fwd:/Fw:/FWD: prefixes and add exactly one."""
    stripped = _FWD_PREFIX_RE.sub("", subject or "").strip()
    return f"Fwd: {stripped}" if stripped else "Fwd:"


@mcp.tool()
def list_folders() -> list[dict]:
    """
    List all mail folders in the Yandex mailbox.

    Returns list of folders with:
    - name: Human-readable folder name (decoded from IMAP UTF-7)
    - imap_name: Raw IMAP folder name (use this for other operations like search_emails)
    - attrs: IMAP folder attributes, e.g. ["\\HasNoChildren", "\\Trash"]
    """
    with imap_connection() as conn:
        status, folder_data = conn.list()
        if status != "OK":
            raise Exception("Failed to list folders")

        folders = []
        for item in folder_data:
            parsed = _parse_folder_line(item)
            if parsed is None:
                continue
            attrs, imap_name = parsed
            folders.append({
                "name": decode_folder_name(imap_name),
                "imap_name": imap_name,
                "attrs": attrs,
            })

        return folders


# Keywords whose next argument is a free-text value and must be IMAP-quoted
# (quoted-string form). All are per RFC 3501 §6.4.4.
_SEARCH_KEYWORDS_STRING_ARG = {
    "FROM", "TO", "CC", "BCC", "SUBJECT", "BODY", "TEXT",
}

# Keywords whose next argument is an atom/number/date and must NOT be quoted.
# RFC 3501 §6.4.4 (SINCE/BEFORE/ON, SENTSINCE/SENTBEFORE/SENTON), RFC 3501
# SEARCH (LARGER/SMALLER), RFC 3501 KEYWORD/UNKEYWORD (flag atoms).
_SEARCH_KEYWORDS_ATOM_ARG = {
    "SINCE", "BEFORE", "ON",
    "SENTSINCE", "SENTBEFORE", "SENTON",
    "LARGER", "SMALLER",
    "KEYWORD", "UNKEYWORD",
    "UID",
}


def build_imap_search_criteria(query: str) -> list[str]:
    """
    Parse user-friendly query into IMAP search criteria with proper quoting.

    Supported keywords:
    - Quoted-string values (auto-quoted):
      FROM, TO, CC, BCC, SUBJECT, BODY, TEXT
    - Atom/number/date values (never quoted):
      SINCE, BEFORE, ON, SENTSINCE, SENTBEFORE, SENTON,
      LARGER, SMALLER, KEYWORD, UNKEYWORD, UID
    - Dual-arg (field name + quoted value):
      HEADER <field> <value>  — e.g. HEADER List-Id "<announce.example>"
      Values containing spaces must be wrapped in double quotes in the
      input: `HEADER X-Custom "multi word value"`
    - Standalone (no args): ALL, UNSEEN, SEEN, ANSWERED, UNANSWERED,
      FLAGGED, UNFLAGGED, DELETED, UNDELETED, DRAFT, UNDRAFT, NEW, OLD, RECENT
    - Logical operators (pass-through, consumer writes proper IMAP form):
      NOT <key>, OR <key1> <key2>, and parenthesized groups (<keys...>)

    Values that already contain quotes in the input are normalized to a
    single pair of outer double quotes.

    Tokenization uses shlex to properly honor quoted strings with spaces
    (e.g. `SUBJECT "hello world"` → `SUBJECT "hello world"`, not broken up).
    """
    if not query or not query.strip() or query.strip().upper() == "ALL":
        return ["ALL"]

    import shlex
    try:
        tokens = shlex.split(query, posix=True)
    except ValueError:
        # Malformed quoting — fall back to naive split
        tokens = query.split()

    if not tokens:
        return ["ALL"]

    result: list[str] = []
    i = 0

    def _normalize_quoted(value: str) -> str:
        """Strip existing outer quotes (single or double) and re-quote."""
        return f'"{value.strip(chr(34) + chr(39))}"'

    while i < len(tokens):
        token = tokens[i]
        upper_token = token.upper()

        if upper_token == "HEADER" and i + 2 < len(tokens):
            # HEADER <field-name> <value>: field is an atom, value is a string.
            field = tokens[i + 1]
            value = tokens[i + 2]
            result.append("HEADER")
            result.append(field)  # atom — no quoting
            result.append(_normalize_quoted(value))
            i += 3
        elif upper_token in _SEARCH_KEYWORDS_STRING_ARG and i + 1 < len(tokens):
            result.append(upper_token)
            result.append(_normalize_quoted(tokens[i + 1]))
            i += 2
        elif upper_token in _SEARCH_KEYWORDS_ATOM_ARG and i + 1 < len(tokens):
            result.append(upper_token)
            result.append(tokens[i + 1])  # atom — no quoting
            i += 2
        else:
            # Pass-through: ALL/UNSEEN/NOT/OR/(…) and already-formatted tokens
            result.append(token)
            i += 1

    return result


@mcp.tool()
def search_emails(
    folder: str = "INBOX",
    query: str = "ALL",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """
    Search emails in a folder.

    IDs returned are IMAP UIDs (stable within a folder's UIDVALIDITY), not
    sequence numbers — they will not change after other messages are deleted.

    Args:
        folder: Mailbox folder (default: INBOX). Use list_folders() to see available folders.
            Accepts either ASCII names, raw IMAP names from list_folders(), or
            human-readable non-ASCII names (e.g. "Корзина") — the latter are
            auto-encoded to IMAP modified UTF-7.
        query: IMAP search query. Examples:
            - "ALL" - all emails
            - "UNSEEN" - unread emails
            - "FROM sender@example.com" - from specific sender
            - "SUBJECT hello" - subject contains "hello"
            - "SINCE 01-Dec-2024" - emails since date
            - "BEFORE 31-Dec-2024" - emails before date
            - Can combine: "UNSEEN FROM boss@company.com"
        limit: Maximum number of emails to return (default: 20)
        offset: Number of newest-first results to skip, for pagination (default: 0)

    Returns list of email summaries with id (UID), subject, from, date.
    """
    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Search emails with properly quoted criteria
        criteria = build_imap_search_criteria(query)

        # Use UTF-8 charset for non-ASCII queries (Cyrillic, etc.)
        has_non_ascii = any(ord(c) > 127 for c in query)
        if has_non_ascii:
            # UID SEARCH with CHARSET: do a legacy (sequence-number) SEARCH
            # via conn.search() which uses imaplib's own charset handling,
            # then translate the returned sequence numbers to UIDs via UID
            # FETCH (UID). This keeps the tool's public contract (UIDs) while
            # avoiding the private _simple_command path for the charset case.
            #
            # Note: conn.search/_simple_command concatenates bytes args raw
            # into the command stream (they are NOT sent as RFC 3501 literals
            # — the literal mechanism uses the conn.literal attribute, which
            # only supports one literal as the last arg). We rely on Yandex
            # being lenient about UTF-8 bytes inside quoted strings, same as
            # the pre-fix code did.
            criteria_str = " ".join(criteria)
            status, seq_data = conn.search("UTF-8", criteria_str.encode("utf-8"))
            if status != "OK":
                raise Exception(f"Search failed: {query}")

            raw_seqs = seq_data[0] if seq_data and seq_data[0] else b""
            seq_ids = (
                raw_seqs.split()
                if isinstance(raw_seqs, (bytes, bytearray))
                else []
            )

            if not seq_ids:
                return []

            # Translate sequence numbers → UIDs via one FETCH round-trip
            seq_set = b",".join(seq_ids).decode("ascii")
            status, uid_data = conn.fetch(seq_set, "(UID)")
            if status != "OK":
                raise Exception("Failed to translate seq numbers to UIDs")

            seq_to_uid: dict[str, str] = {}
            for item in uid_data or []:
                if not isinstance(item, (bytes, bytearray)):
                    continue
                line = item.decode("ascii", errors="ignore")
                # Format varies by imaplib version and response shape:
                #   "5 (UID 1023)"
                #   "* 5 FETCH (UID 1023)"
                #   "5 (UID 1023 FLAGS (\\Seen))"
                # Tokenize after removing parens and find UID by label,
                # not by positional index.
                tokens = line.replace("(", " ").replace(")", " ").split()
                upper = [t.upper() for t in tokens]
                if "UID" not in upper:
                    continue
                uid_idx = upper.index("UID")
                if uid_idx + 1 >= len(tokens):
                    continue
                # First numeric token is the sequence number
                seq_token = next(
                    (t for t in tokens if t.isdigit()), None
                )
                if seq_token is None:
                    continue
                seq_to_uid[seq_token] = tokens[uid_idx + 1]

            # Preserve original search order
            ids = [
                seq_to_uid[s.decode("ascii")].encode("ascii")
                for s in seq_ids
                if s.decode("ascii") in seq_to_uid
            ]
            # Short-circuit the common post-processing path below
            message_ids = [b" ".join(ids)]
        else:
            status, message_ids = conn.uid("SEARCH", *criteria)

        if status != "OK":
            raise Exception(f"Search failed: {query}")

        # message_ids[0] may be None if the response had no SEARCH line
        raw_ids = message_ids[0] if message_ids and message_ids[0] else b""
        ids = raw_ids.split() if isinstance(raw_ids, (bytes, bytearray)) else []

        # Newest-first, then paginate via offset + limit
        ids = list(reversed(ids))
        ids = ids[offset : offset + limit]

        emails = []
        for uid_bytes in ids:
            uid_str = uid_bytes.decode("ascii") if isinstance(uid_bytes, (bytes, bytearray)) else str(uid_bytes)
            # Fetch headers only for performance
            status, msg_data = conn.uid(
                "FETCH",
                uid_str,
                "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])",
            )
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            raw_header = msg_data[0][1]
            msg = email.message_from_bytes(raw_header)

            subject = decode_mime_header(msg.get("Subject", ""))
            from_addr = decode_mime_header(msg.get("From", ""))
            date_str = msg.get("Date", "")

            emails.append({
                "id": uid_str,
                "subject": subject,
                "from": from_addr,
                "date": date_str,
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
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.uid("FETCH", email_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
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
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_text = payload.decode(charset, errors="replace")
                elif content_type == "text/html" and not body_html:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_html = payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                if msg.get_content_type() == "text/html":
                    body_html = payload.decode(charset, errors="replace")
                else:
                    body_text = payload.decode(charset, errors="replace")

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
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.uid("FETCH", email_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
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
                        # Sanitize: email-provided filenames are untrusted.
                        # Strip any path components to prevent path traversal
                        # (e.g. "../../.bashrc" → ".bashrc").
                        safe_name = Path(decoded_filename).name
                        if not safe_name or safe_name in (".", ".."):
                            raise ValueError(
                                f"Invalid attachment filename: {decoded_filename}"
                            )

                        save_root = save_path.resolve()
                        file_path = (save_root / safe_name).resolve()

                        # Defensive: ensure the resolved path stays within save_dir
                        try:
                            file_path.relative_to(save_root)
                        except ValueError:
                            raise ValueError(
                                f"Attachment path escapes save directory: {decoded_filename}"
                            )

                        with open(file_path, "wb") as f:
                            f.write(payload)

                        return {
                            "status": "downloaded",
                            "filename": safe_name,
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
    html: bool = False,
    attachments: Optional[list[str]] = None,
    save_to_sent: bool = True,
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
        attachments: Optional list of absolute file paths to attach. Each
            attachment is resolved and must be a regular file. SECURITY: this
            tool can read any file accessible to the MCP server process and
            exfiltrate it via email — the MCP client should surface every
            send_email call for user approval. All attached paths are logged
            to yandex_mail_mcp.log for audit.
        save_to_sent: If True (default), append a copy of the sent message to
            the Sent folder via IMAP APPEND. Yandex does not reliably auto-save
            SMTP-sent messages to Sent; this ensures a copy exists. Failure to
            save is non-fatal and logged as a warning.

    Returns confirmation with recipients, attached file names, and
    saved_to_sent (decoded Sent folder name or None).
    """
    msg, attached_names = _build_message(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        html=html,
        attachments=attachments,
    )

    # Build recipient list for the SMTP envelope
    recipients = [addr.strip() for addr in to.split(",")]
    if cc:
        recipients.extend([addr.strip() for addr in cc.split(",")])
    if bcc:
        recipients.extend([addr.strip() for addr in bcc.split(",")])

    with smtp_connection() as conn:
        conn.send_message(msg, EMAIL, recipients)

    saved = None
    if save_to_sent:
        saved = _save_to_sent_folder(msg)

    return {
        "status": "sent",
        "to": to,
        "subject": subject,
        "cc": cc,
        "bcc": bcc,
        "attachments": attached_names,
        "saved_to_sent": saved,
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
    encoded_source = encode_folder_name(folder)
    encoded_dest = encode_folder_name(destination)

    with imap_connection() as conn:
        status, _ = conn.select(encoded_source)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Prefer atomic UID MOVE (RFC 6851) when the server advertises it.
        if _has_capability(conn, "MOVE"):
            try:
                typ, _ = conn._simple_command(
                    "UID", "MOVE", email_id,
                    _quote_folder_for_command(encoded_dest),
                )
                if typ == "OK":
                    return {
                        "status": "moved",
                        "email_id": email_id,
                        "from_folder": folder,
                        "to_folder": destination,
                        "method": "MOVE",
                    }
                logger.warning(
                    "UID MOVE returned %s, falling back to COPY+STORE", typ
                )
            except Exception as e:
                logger.warning("UID MOVE failed, falling back to COPY: %s", e)

        # Fallback: COPY → +FLAGS \Deleted → EXPUNGE
        status, _ = conn.uid("COPY", email_id, encoded_dest)
        if status != "OK":
            raise Exception(f"Failed to copy email to: {destination}")

        status, _ = conn.uid("STORE", email_id, "+FLAGS", "\\Deleted")
        if status != "OK":
            raise Exception("Failed to mark original as deleted")

        conn.expunge()

        return {
            "status": "moved",
            "email_id": email_id,
            "from_folder": folder,
            "to_folder": destination,
            "method": "COPY+STORE+EXPUNGE",
        }


@mcp.tool()
def delete_email(folder: str, email_id: str) -> dict:
    """
    Delete an email (move to Trash).

    Trash folder is discovered via the IMAP \\Trash SPECIAL-USE attribute
    (RFC 6154) with fallbacks to common localized names. If no trash folder
    is found or copy fails, the email is permanently deleted.

    Args:
        folder: Folder containing the email
        email_id: Email ID to delete

    Returns confirmation of deletion.
    """
    with imap_connection() as conn:
        # Resolve trash folder before selecting the source
        trash_folder = _find_trash_folder(conn)

        status, _ = conn.select(encode_folder_name(folder))
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Don't try to copy INTO the folder we're already in. Compare the
        # decoded human-readable forms with casefold so "trash" == "Trash"
        # and different but equivalent UTF-7 encodings match.
        folder_key = decode_folder_name(encode_folder_name(folder)).strip().casefold()
        trash_key = decode_folder_name(trash_folder).strip().casefold()
        same_folder = folder_key == trash_key

        if not same_folder:
            # Try atomic MOVE extension first
            if _has_capability(conn, "MOVE"):
                try:
                    typ, _ = conn._simple_command(
                        "UID", "MOVE", email_id,
                        _quote_folder_for_command(trash_folder),
                    )
                    if typ == "OK":
                        return {
                            "status": "moved_to_trash",
                            "email_id": email_id,
                            "folder": folder,
                            "trash_folder": decode_folder_name(trash_folder),
                            "method": "MOVE",
                        }
                    logger.warning(
                        "UID MOVE to trash returned %s, falling back", typ
                    )
                except Exception as e:
                    logger.warning(
                        "UID MOVE to trash failed, falling back: %s", e
                    )

            # Fallback: COPY → +FLAGS \Deleted → EXPUNGE
            status, _ = conn.uid("COPY", email_id, trash_folder)
            if status != "OK":
                # Trash copy failed — fall through to permanent delete
                status, _ = conn.uid("STORE", email_id, "+FLAGS", "\\Deleted")
                if status != "OK":
                    raise Exception("Failed to delete email")
                conn.expunge()
                return {
                    "status": "deleted_permanently",
                    "email_id": email_id,
                    "folder": folder,
                }

            conn.uid("STORE", email_id, "+FLAGS", "\\Deleted")
            conn.expunge()
            return {
                "status": "moved_to_trash",
                "email_id": email_id,
                "folder": folder,
                "trash_folder": decode_folder_name(trash_folder),
                "method": "COPY+STORE+EXPUNGE",
            }

        # Already in Trash — permanent delete
        status, _ = conn.uid("STORE", email_id, "+FLAGS", "\\Deleted")
        if status != "OK":
            raise Exception("Failed to delete email")
        conn.expunge()
        return {
            "status": "deleted_permanently",
            "email_id": email_id,
            "folder": folder,
        }


@mcp.tool()
def set_flags(
    folder: str,
    email_id: str,
    add: Optional[list[str]] = None,
    remove: Optional[list[str]] = None,
) -> dict:
    """
    Set or clear IMAP flags on a message.

    Common IMAP system flags (backslash-prefixed): \\Seen, \\Flagged,
    \\Answered, \\Draft, \\Deleted. Custom user keywords have no backslash.

    Args:
        folder: Folder containing the email
        email_id: UID of the email (from search_emails result)
        add: Flags to add (e.g. ["\\Seen", "\\Flagged"])
        remove: Flags to remove

    Returns confirmation with the flags added/removed.
    """
    return _set_flags_impl(folder, email_id, add=add, remove=remove)


@mcp.tool()
def mark_read(folder: str, email_id: str) -> dict:
    """Mark an email as read (adds \\Seen)."""
    return _set_flags_impl(folder, email_id, add=["\\Seen"])


@mcp.tool()
def mark_unread(folder: str, email_id: str) -> dict:
    """Mark an email as unread (removes \\Seen)."""
    return _set_flags_impl(folder, email_id, remove=["\\Seen"])


@mcp.tool()
def mark_flagged(folder: str, email_id: str, flagged: bool = True) -> dict:
    """Star or unstar an email via the \\Flagged IMAP flag."""
    if flagged:
        return _set_flags_impl(folder, email_id, add=["\\Flagged"])
    return _set_flags_impl(folder, email_id, remove=["\\Flagged"])


@mcp.tool()
def mark_answered(folder: str, email_id: str) -> dict:
    """Mark an email as answered (adds \\Answered)."""
    return _set_flags_impl(folder, email_id, add=["\\Answered"])


@mcp.tool()
def get_folder_status(folder: str) -> dict:
    """
    Get counts and state for a folder via IMAP STATUS (RFC 3501).

    Returns dict with keys (when available):
    - folder: input folder name
    - messages: total messages
    - unseen: unread messages
    - recent: recent messages
    - uidnext: next UID to be assigned
    - uidvalidity: UID validity identifier (if this changes, stored UIDs
      are no longer valid and must be re-fetched)
    """
    items = "(MESSAGES UNSEEN RECENT UIDNEXT UIDVALIDITY)"
    with imap_connection() as conn:
        status, data = conn.status(encode_folder_name(folder), items)
        if status != "OK":
            raise Exception(f"Failed to get status for: {folder}")

        result: dict = {"folder": folder}

        if not data or not data[0]:
            return result

        raw = data[0]
        if isinstance(raw, (bytes, bytearray)):
            response = raw.decode("utf-8", errors="replace")
        else:
            response = str(raw)

        # Format: "FolderName" (KEY1 val1 KEY2 val2 ...)
        start = response.find("(")
        end = response.rfind(")")
        if start < 0 or end < 0 or end <= start:
            return result

        parts = response[start + 1 : end].split()
        for i in range(0, len(parts) - 1, 2):
            key = parts[i].lower()
            value_token = parts[i + 1]
            try:
                result[key] = int(value_token)
            except ValueError:
                result[key] = value_token

        return result


@mcp.tool()
def create_folder(name: str) -> dict:
    """
    Create a new mail folder. Name can be human-readable (Cyrillic supported —
    auto-encoded to IMAP modified UTF-7).
    """
    with imap_connection() as conn:
        status, _ = conn.create(encode_folder_name(name))
        if status != "OK":
            raise Exception(f"Failed to create folder: {name}")
        return {"status": "created", "folder": name}


@mcp.tool()
def rename_folder(old_name: str, new_name: str) -> dict:
    """Rename a mail folder. Both names are auto-encoded to UTF-7."""
    with imap_connection() as conn:
        status, _ = conn.rename(
            encode_folder_name(old_name),
            encode_folder_name(new_name),
        )
        if status != "OK":
            raise Exception(
                f"Failed to rename folder: {old_name} → {new_name}"
            )
        return {
            "status": "renamed",
            "old_name": old_name,
            "new_name": new_name,
        }


@mcp.tool()
def delete_folder(name: str) -> dict:
    """
    Delete a mail folder.

    WARNING: This is destructive. Behavior on non-empty folders is
    server-dependent (RFC 3501 §6.3.4 permits servers to return NO);
    some servers reject the operation, others delete the contents
    without moving them to Trash. The MCP client should surface this
    call for user approval.
    """
    with imap_connection() as conn:
        status, _ = conn.delete(encode_folder_name(name))
        if status != "OK":
            raise Exception(f"Failed to delete folder: {name}")
        return {"status": "deleted", "folder": name}


@mcp.tool()
def reply_email(
    folder: str,
    email_id: str,
    body: str,
    reply_all: bool = False,
    html: bool = False,
    attachments: Optional[list[str]] = None,
    save_to_sent: bool = True,
) -> dict:
    """
    Reply to an email with correct threading headers (RFC 5322).

    Fetches the original message's Message-ID, References, Subject, From,
    Reply-To, To and Cc headers, and builds a reply with:
    - `In-Reply-To` pointing at the original Message-ID
    - `References` chaining the previous thread plus the original Message-ID
    - `Subject` with a deduped "Re: " prefix
    - Recipients: original Reply-To (or From); if reply_all, also original
      To + Cc with our own address removed

    Args:
        folder: Folder containing the original email
        email_id: UID of the email to reply to
        body: Reply body text (plain or HTML)
        reply_all: If True, include original To + Cc recipients
        html: If True, body is HTML
        attachments: Optional list of file paths to attach
        save_to_sent: If True (default), save the reply to the Sent folder
    """
    if not EMAIL:
        raise ValueError("YANDEX_EMAIL must be set in .env")

    # Fetch original's threading headers in a single small request
    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.uid(
            "FETCH",
            email_id,
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES SUBJECT FROM REPLY-TO TO CC)])",
        )
        if status != "OK" or not msg_data or not msg_data[0]:
            raise Exception(f"Failed to fetch original email: {email_id}")

        raw = msg_data[0][1]
        original = email.message_from_bytes(raw)

    original_msg_id = (original.get("Message-ID") or "").strip()
    original_refs = (original.get("References") or "").strip()
    original_subject = decode_mime_header(original.get("Subject", ""))
    reply_to_header = original.get("Reply-To") or original.get("From") or ""
    original_to = original.get("To", "")
    original_cc = original.get("Cc", "")

    # Reply recipients
    to_addr = reply_to_header
    cc_addr: Optional[str] = None
    if reply_all:
        # Parse original To + Cc as proper RFC 5322 address lists. getaddresses
        # correctly handles display names with commas, quoted-printable encoding
        # and multiple addresses per header.
        all_addrs: list[tuple[str, str]] = list(
            getaddresses([original_to, original_cc])
        )
        own = (EMAIL or "").lower()
        filtered: list[str] = []
        for display_name, bare_addr in all_addrs:
            if not bare_addr:
                continue
            # Exact address match, case-insensitive
            if bare_addr.lower() == own:
                continue
            filtered.append(formataddr((display_name, bare_addr)))
        if filtered:
            cc_addr = ", ".join(filtered)

    # Threading headers
    extra_headers: dict = {}
    if original_msg_id:
        extra_headers["In-Reply-To"] = original_msg_id
        chained = (
            f"{original_refs} {original_msg_id}".strip()
            if original_refs
            else original_msg_id
        )
        extra_headers["References"] = _trim_references(chained)
    else:
        logger.warning(
            "reply_email: original %s has no Message-ID, thread will break",
            email_id,
        )

    reply_subject = _dedupe_re_prefix(original_subject)

    msg, attached_names = _build_message(
        to=to_addr,
        subject=reply_subject,
        body=body,
        cc=cc_addr,
        html=html,
        attachments=attachments,
        extra_headers=extra_headers,
    )

    recipients = [a.strip() for a in to_addr.split(",") if a.strip()]
    if cc_addr:
        recipients.extend(a.strip() for a in cc_addr.split(",") if a.strip())

    with smtp_connection() as conn:
        conn.send_message(msg, EMAIL, recipients)

    # Also mark the original as answered (best-effort; failure is non-fatal)
    try:
        _set_flags_impl(folder, email_id, add=["\\Answered"])
    except Exception as e:
        logger.warning("reply_email: failed to mark original as answered: %s", e)

    saved = None
    if save_to_sent:
        saved = _save_to_sent_folder(msg)

    return {
        "status": "sent",
        "reply_to": to_addr,
        "cc": cc_addr,
        "subject": reply_subject,
        "in_reply_to": original_msg_id or None,
        "attachments": attached_names,
        "saved_to_sent": saved,
    }


@mcp.tool()
def forward_email(
    folder: str,
    email_id: str,
    to: str,
    body: str = "",
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False,
    attachments: Optional[list[str]] = None,
    as_attachment: bool = True,
    save_to_sent: bool = True,
) -> dict:
    """
    Forward an email to new recipients.

    Unlike reply_email, this is a new thread: no In-Reply-To or References
    headers are set, and the subject gets a deduped "Fwd: " prefix.

    Args:
        folder: Folder containing the original email
        email_id: UID of the email to forward
        to: Forward recipients (comma-separated)
        body: Optional introduction text prepended to the forwarded content
        cc: CC recipients (comma-separated)
        bcc: BCC recipients (comma-separated)
        html: If True, intro body is HTML (affects inline display only)
        attachments: Additional files to attach alongside the original
        as_attachment: If True (default), original message is attached as
            message/rfc822 (preserves all original headers and structure).
            If False, headers + body are inlined as quoted text in the body.
        save_to_sent: If True (default), save a copy to the Sent folder
    """
    if not EMAIL:
        raise ValueError("YANDEX_EMAIL must be set in .env")

    # Fetch the full original message
    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.uid("FETCH", email_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise Exception(f"Failed to fetch original email: {email_id}")

        raw_original = msg_data[0][1]
        original = email.message_from_bytes(raw_original)

    original_subject = decode_mime_header(original.get("Subject", ""))
    forward_subject = _dedupe_fwd_prefix(original_subject)

    if as_attachment:
        # Build outer multipart/mixed with body + message/rfc822 + extra attachments
        from email.mime.message import MIMEMessage

        outer = MIMEMultipart("mixed")
        body_subtype = "html" if html else "plain"
        outer.attach(MIMEText(body or "", body_subtype, "utf-8"))

        # Attach original as message/rfc822. Sanitize the filename: subject
        # may contain path separators, quotes, control chars, or be arbitrarily
        # long — any of these corrupt the Content-Disposition header.
        safe_subject = re.sub(
            r'[\\/:*?"<>|\x00-\x1f]', "_", original_subject or "message"
        ).strip() or "message"
        safe_subject = safe_subject[:80]
        rfc822_part = MIMEMessage(original)
        rfc822_part.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"{safe_subject}.eml",
        )
        outer.attach(rfc822_part)

        # Additional user-supplied attachments
        attached_names: list[str] = []
        for filepath in attachments or []:
            path = Path(filepath).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Attachment not found: {filepath}")
            logger.info("forward_email attaching file: %s", path)
            with open(path, "rb") as f:
                data = f.read()
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", "attachment", filename=path.name
            )
            outer.attach(part)
            attached_names.append(path.name)

        outer["Subject"] = forward_subject
        outer["From"] = EMAIL
        outer["To"] = to
        if cc:
            outer["Cc"] = cc
        msg = outer
    else:
        # Inline: build quoted body with original headers + text
        orig_from = decode_mime_header(original.get("From", ""))
        orig_to = decode_mime_header(original.get("To", ""))
        orig_date = original.get("Date", "")
        orig_subject = original_subject

        # Extract original plain text body (best effort)
        orig_body_text = ""
        if original.is_multipart():
            for part in original.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        orig_body_text = payload.decode(charset, errors="replace")
                        break
        else:
            payload = original.get_payload(decode=True)
            if payload:
                charset = original.get_content_charset() or "utf-8"
                orig_body_text = payload.decode(charset, errors="replace")

        quoted = "\n".join("> " + line for line in orig_body_text.splitlines())
        inline_body = (
            f"{body}\n\n"
            f"---------- Forwarded message ----------\n"
            f"From: {orig_from}\n"
            f"Date: {orig_date}\n"
            f"Subject: {orig_subject}\n"
            f"To: {orig_to}\n\n"
            f"{quoted}"
        )
        msg, attached_names = _build_message(
            to=to,
            subject=forward_subject,
            body=inline_body,
            cc=cc,
            html=html,
            attachments=attachments,
        )

    # Recipients for SMTP envelope
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    if cc:
        recipients.extend(a.strip() for a in cc.split(",") if a.strip())
    if bcc:
        recipients.extend(a.strip() for a in bcc.split(",") if a.strip())

    with smtp_connection() as conn:
        conn.send_message(msg, EMAIL, recipients)

    saved = None
    if save_to_sent:
        saved = _save_to_sent_folder(msg)

    return {
        "status": "sent",
        "forwarded_to": to,
        "cc": cc,
        "subject": forward_subject,
        "mode": "attachment" if as_attachment else "inline",
        "extra_attachments": attached_names,
        "saved_to_sent": saved,
    }


@mcp.tool()
def bulk_set_flags(
    folder: str,
    email_ids: list[str],
    add: Optional[list[str]] = None,
    remove: Optional[list[str]] = None,
) -> dict:
    """
    Set or clear IMAP flags on multiple messages in a single operation.

    More efficient than looping set_flags: one UID STORE per chunk of ~500
    UIDs, not one per message. Validates every flag the same way set_flags
    does (rejects flags with whitespace, parens, etc.).

    Args:
        folder: Folder containing the messages
        email_ids: List of UIDs to update
        add: Flags to add (e.g. ["\\Seen"])
        remove: Flags to remove

    Returns count of UIDs touched per add/remove operation.
    """
    if not add and not remove:
        raise ValueError("At least one of add/remove must be non-empty")

    _bad_flag_chars = set(' \t\r\n"\\()[]{}%*')
    for flag in list(add or []) + list(remove or []):
        if not flag or not isinstance(flag, str):
            raise ValueError(f"Invalid flag: {flag!r}")
        body = flag[1:] if flag.startswith("\\") else flag
        if not body or any(c in _bad_flag_chars for c in body):
            raise ValueError(
                f"Invalid flag {flag!r}: contains reserved IMAP characters"
            )

    uids = _normalize_uid_list(email_ids)

    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder))
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        for chunk in _chunk(uids, _BULK_UID_CHUNK):
            uid_set = ",".join(chunk)
            if add:
                flag_list = " ".join(add)
                status, _ = conn.uid(
                    "STORE", uid_set, "+FLAGS", f"({flag_list})"
                )
                if status != "OK":
                    raise Exception(
                        f"bulk_set_flags: failed to add flags {flag_list}"
                    )
            if remove:
                flag_list = " ".join(remove)
                status, _ = conn.uid(
                    "STORE", uid_set, "-FLAGS", f"({flag_list})"
                )
                if status != "OK":
                    raise Exception(
                        f"bulk_set_flags: failed to remove flags {flag_list}"
                    )

    return {
        "status": "ok",
        "folder": folder,
        "count": len(uids),
        "added": list(add or []),
        "removed": list(remove or []),
    }


@mcp.tool()
def bulk_mark_read(folder: str, email_ids: list[str]) -> dict:
    """Mark multiple emails as read (adds \\Seen)."""
    return bulk_set_flags(folder, email_ids, add=["\\Seen"])


@mcp.tool()
def bulk_mark_unread(folder: str, email_ids: list[str]) -> dict:
    """Mark multiple emails as unread (removes \\Seen)."""
    return bulk_set_flags(folder, email_ids, remove=["\\Seen"])


@mcp.tool()
def bulk_mark_flagged(
    folder: str, email_ids: list[str], flagged: bool = True
) -> dict:
    """Star or unstar multiple emails via the \\Flagged flag."""
    if flagged:
        return bulk_set_flags(folder, email_ids, add=["\\Flagged"])
    return bulk_set_flags(folder, email_ids, remove=["\\Flagged"])


@mcp.tool()
def bulk_move(
    folder: str, email_ids: list[str], destination: str
) -> dict:
    """
    Move multiple messages to another folder in a single IMAP session.

    Uses atomic UID MOVE (RFC 6851) in chunks when the server advertises it,
    falls back to COPY+STORE+EXPUNGE per chunk otherwise.
    """
    uids = _normalize_uid_list(email_ids)
    encoded_source = encode_folder_name(folder)
    encoded_dest = encode_folder_name(destination)
    quoted_dest = _quote_folder_for_command(encoded_dest)

    with imap_connection() as conn:
        status, _ = conn.select(encoded_source)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        use_move = _has_capability(conn, "MOVE")
        method = "MOVE" if use_move else "COPY+STORE+EXPUNGE"

        for chunk in _chunk(uids, _BULK_UID_CHUNK):
            uid_set = ",".join(chunk)
            if use_move:
                try:
                    typ, _ = conn._simple_command(
                        "UID", "MOVE", uid_set, quoted_dest
                    )
                    if typ == "OK":
                        continue
                    logger.warning(
                        "bulk_move: UID MOVE returned %s, falling back", typ
                    )
                    use_move = False
                    method = "COPY+STORE+EXPUNGE"
                except Exception as e:
                    logger.warning(
                        "bulk_move: UID MOVE failed, falling back: %s", e
                    )
                    use_move = False
                    method = "COPY+STORE+EXPUNGE"

            # Fallback path (also reached if MOVE flipped mid-loop)
            status, _ = conn.uid("COPY", uid_set, encoded_dest)
            if status != "OK":
                raise Exception(
                    f"bulk_move: failed to copy chunk to {destination}"
                )
            conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")

        if not use_move:
            conn.expunge()

    return {
        "status": "moved",
        "from_folder": folder,
        "to_folder": destination,
        "count": len(uids),
        "method": method,
    }


@mcp.tool()
def bulk_delete(
    folder: str, email_ids: list[str], permanent: bool = False
) -> dict:
    """
    Delete multiple messages at once.

    If permanent=False (default), moves to Trash (discovered via \\Trash
    SPECIAL-USE with localized fallbacks). If permanent=True or no Trash
    folder is found, marks +FLAGS \\Deleted and EXPUNGEs immediately.

    Deleting from within the Trash folder is always permanent regardless
    of the flag.
    """
    uids = _normalize_uid_list(email_ids)

    with imap_connection() as conn:
        trash_folder = _find_trash_folder(conn)

        status, _ = conn.select(encode_folder_name(folder))
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # If we're already in Trash, or permanent=True, just flag+expunge.
        folder_key = (
            decode_folder_name(encode_folder_name(folder)).strip().casefold()
        )
        trash_key = decode_folder_name(trash_folder).strip().casefold()
        force_permanent = permanent or folder_key == trash_key

        if force_permanent:
            for chunk in _chunk(uids, _BULK_UID_CHUNK):
                uid_set = ",".join(chunk)
                status, _ = conn.uid(
                    "STORE", uid_set, "+FLAGS", "(\\Deleted)"
                )
                if status != "OK":
                    raise Exception("bulk_delete: failed to mark as deleted")
            conn.expunge()
            return {
                "status": "deleted_permanently",
                "folder": folder,
                "count": len(uids),
            }

        # Move to Trash (MOVE if supported, COPY+STORE+EXPUNGE otherwise)
        encoded_trash = trash_folder
        quoted_trash = _quote_folder_for_command(encoded_trash)
        use_move = _has_capability(conn, "MOVE")
        method = "MOVE" if use_move else "COPY+STORE+EXPUNGE"

        for chunk in _chunk(uids, _BULK_UID_CHUNK):
            uid_set = ",".join(chunk)
            if use_move:
                try:
                    typ, _ = conn._simple_command(
                        "UID", "MOVE", uid_set, quoted_trash
                    )
                    if typ == "OK":
                        continue
                    use_move = False
                    method = "COPY+STORE+EXPUNGE"
                except Exception as e:
                    logger.warning(
                        "bulk_delete: UID MOVE failed, falling back: %s", e
                    )
                    use_move = False
                    method = "COPY+STORE+EXPUNGE"

            status, _ = conn.uid("COPY", uid_set, encoded_trash)
            if status != "OK":
                raise Exception("bulk_delete: failed to copy to trash")
            conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")

        if not use_move:
            conn.expunge()

    return {
        "status": "moved_to_trash",
        "folder": folder,
        "trash_folder": decode_folder_name(trash_folder),
        "count": len(uids),
        "method": method,
    }


@mcp.tool()
def inspect_email(folder: str, email_id: str) -> dict:
    """
    Inspect an email's headers and MIME structure WITHOUT downloading bodies.

    Uses FETCH BODYSTRUCTURE + header subset — returns in milliseconds even
    for huge messages with attachments. Ideal for:
    - Previewing large emails without tying up bandwidth
    - Deciding which attachment to download (see fetch_part)
    - Bulk processing many messages efficiently

    Returns subject/from/to/date/size plus a list of MIME parts, each with:
    - part: part number (e.g. "1", "2", "2.1") — pass to fetch_part
    - type: MIME type (e.g. "text/plain", "application/pdf")
    - size: part size in bytes (may be None)
    - charset: for text parts
    - filename: for attachments (RFC 2231 / MIME decoded)
    - disposition: "inline" or "attachment"
    """
    fetch_items = (
        "(RFC822.SIZE BODYSTRUCTURE "
        "BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])"
    )
    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.uid("FETCH", email_id, fetch_items)
        if status != "OK" or not msg_data:
            raise Exception(f"Failed to inspect email: {email_id}")

    # IMAP FETCH response layout for a multi-item request is messy. Each
    # "literal" section comes back as a tuple (envelope_line, literal_bytes)
    # where envelope_line tells us which section the literal belongs to.
    # Non-literal items come back as bare bytes lines.
    #
    # We need to tell apart the HEADER.FIELDS literal from a BODYSTRUCTURE
    # literal (large structures may arrive as literals). The envelope line
    # for a HEADER.FIELDS literal includes "BODY[HEADER..." or
    # "BODY[HEADER.FIELDS..."; the BODYSTRUCTURE itself is usually inline
    # inside the envelope line, but servers may push it as a literal too.
    header_bytes = b""
    bodystructure_raw = b""
    total_size: Optional[int] = None

    def _extract_size(text: str) -> None:
        nonlocal total_size
        if total_size is None:
            m = re.search(r"RFC822\.SIZE\s+(\d+)", text)
            if m:
                total_size = int(m.group(1))

    def _extract_inline_bodystructure(env_bytes: bytes) -> bytes:
        """
        Pull an inline BODYSTRUCTURE s-expression out of an envelope line.

        Walks the byte stream with a paren-depth counter, skipping parens
        that live inside quoted strings, so we extract exactly the balanced
        s-expression and none of the surrounding FETCH response tokens
        (e.g. a trailing ` BODY[HEADER.FIELDS ...] {N}`).
        """
        env_str = env_bytes.decode("utf-8", errors="replace")
        idx = env_str.upper().find("BODYSTRUCTURE")
        if idx < 0:
            return b""
        paren_idx = env_str.find("(", idx)
        if paren_idx < 0:
            return b""

        depth = 0
        in_quote = False
        j = paren_idx
        n = len(env_bytes)
        while j < n:
            b = env_bytes[j : j + 1]
            if in_quote:
                if b == b"\\" and j + 1 < n:
                    j += 2
                    continue
                if b == b'"':
                    in_quote = False
                j += 1
                continue
            if b == b'"':
                in_quote = True
            elif b == b"(":
                depth += 1
            elif b == b")":
                depth -= 1
                if depth == 0:
                    return env_bytes[paren_idx : j + 1]
            j += 1
        return b""  # unbalanced — better empty than garbage

    for item in msg_data:
        if isinstance(item, tuple) and len(item) >= 2:
            envelope = item[0]
            payload = item[1]
            if not isinstance(envelope, (bytes, bytearray)):
                continue
            env_str = envelope.decode("utf-8", errors="replace")
            _extract_size(env_str)

            env_upper = env_str.upper()
            is_header_literal = (
                "BODY[HEADER" in env_upper or "BODY.PEEK[HEADER" in env_upper
            )
            is_bs_literal = (
                "BODYSTRUCTURE" in env_upper
                and not bodystructure_raw
                and env_str.rstrip().endswith("}")
                # The envelope ends with a literal-size marker like `{89}`
                # when the FOLLOWING payload is the literal.
            )

            if is_header_literal and isinstance(payload, (bytes, bytearray)):
                header_bytes = bytes(payload)
            elif is_bs_literal and isinstance(payload, (bytes, bytearray)):
                bodystructure_raw = bytes(payload)
            else:
                # Inline BODYSTRUCTURE inside this envelope line
                inline = _extract_inline_bodystructure(envelope)
                if inline and not bodystructure_raw:
                    bodystructure_raw = inline
                # Payload here is likely the body of some other section;
                # if we haven't seen a header literal yet, keep it as a
                # fallback for the header parse attempt.
                if isinstance(payload, (bytes, bytearray)) and not header_bytes:
                    header_bytes = bytes(payload)
        elif isinstance(item, (bytes, bytearray)):
            s = item.decode("utf-8", errors="replace")
            _extract_size(s)
            if not bodystructure_raw:
                inline = _extract_inline_bodystructure(bytes(item))
                if inline:
                    bodystructure_raw = inline

    msg = email.message_from_bytes(header_bytes) if header_bytes else None
    subject = decode_mime_header(msg.get("Subject", "")) if msg else ""
    from_addr = decode_mime_header(msg.get("From", "")) if msg else ""
    to_addr = decode_mime_header(msg.get("To", "")) if msg else ""
    date_str = msg.get("Date", "") if msg else ""

    parts = parse_bodystructure(bodystructure_raw) if bodystructure_raw else []

    return {
        "id": email_id,
        "subject": subject,
        "from": from_addr,
        "to": to_addr,
        "date": date_str,
        "size": total_size,
        "parts": parts,
    }


@mcp.tool()
def fetch_part(
    folder: str,
    email_id: str,
    part_number: str,
    decode: bool = True,
) -> dict:
    """
    Fetch a specific MIME part of an email by part number.

    Part numbers come from inspect_email's `parts` list (e.g. "1", "2.1").
    For text parts with decode=True (default), returns the decoded string
    body. For binary parts or decode=False, returns base64-encoded bytes
    so the result is JSON-safe.

    Args:
        folder: Folder containing the email
        email_id: UID of the email
        part_number: Part identifier from inspect_email (e.g. "1", "2.1")
        decode: If True, decode text parts to str; otherwise return base64

    Returns dict with `content` (str or base64) + `encoding` marker.
    """
    import base64

    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", part_number or ""):
        raise ValueError(f"Invalid part_number: {part_number!r}")

    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Fetch the specific part and its MIME header (for charset)
        fetch_spec = f"(BODY.PEEK[{part_number}] BODY.PEEK[{part_number}.MIME])"
        status, msg_data = conn.uid("FETCH", email_id, fetch_spec)
        if status != "OK" or not msg_data:
            raise Exception(f"Failed to fetch part {part_number}")

    body_bytes = b""
    mime_header_bytes = b""
    for item in msg_data:
        if isinstance(item, tuple) and len(item) >= 2:
            envelope = item[0]
            payload = item[1]
            if not isinstance(envelope, (bytes, bytearray)) or not isinstance(
                payload, (bytes, bytearray)
            ):
                continue
            env_str = envelope.decode("utf-8", errors="replace")
            # envelope contains e.g. "<uid> BODY[1] {123}" or similar
            if ".MIME" in env_str.upper():
                mime_header_bytes = bytes(payload)
            else:
                body_bytes = bytes(payload)

    # Parse MIME header for encoding + charset
    encoding_hdr = ""
    charset = "utf-8"
    if mime_header_bytes:
        hdr = email.message_from_bytes(mime_header_bytes)
        encoding_hdr = (hdr.get("Content-Transfer-Encoding") or "").lower()
        ctype = hdr.get_content_type() or ""
        charset = hdr.get_content_charset() or charset

    # Decode transfer encoding
    payload_bytes = body_bytes
    if encoding_hdr == "base64":
        try:
            payload_bytes = base64.b64decode(body_bytes)
        except Exception:
            pass
    elif encoding_hdr == "quoted-printable":
        import quopri
        try:
            payload_bytes = quopri.decodestring(body_bytes)
        except Exception:
            pass

    if decode:
        try:
            return {
                "part": part_number,
                "encoding": "text",
                "charset": charset,
                "content": payload_bytes.decode(charset, errors="replace"),
                "size": len(payload_bytes),
            }
        except Exception:
            pass

    return {
        "part": part_number,
        "encoding": "base64",
        "content": base64.b64encode(payload_bytes).decode("ascii"),
        "size": len(payload_bytes),
    }


@mcp.tool()
def empty_trash() -> dict:
    """
    Empty the Trash folder.

    Discovers Trash via \\Trash SPECIAL-USE with localized fallbacks, selects
    it, marks all messages +FLAGS \\Deleted, and EXPUNGEs. Returns the count
    of deleted messages.
    """
    with imap_connection() as conn:
        trash = _find_trash_folder(conn)
        human_name = decode_folder_name(trash)

        status, _ = conn.select(trash)
        if status != "OK":
            raise Exception(f"Failed to select Trash folder: {human_name}")

        # Find all UIDs in the trash folder
        status, data = conn.uid("SEARCH", "ALL")
        if status != "OK":
            raise Exception(f"Failed to enumerate Trash contents")

        raw = data[0] if data and data[0] else b""
        uids = (
            raw.split() if isinstance(raw, (bytes, bytearray)) else []
        )

        if not uids:
            return {
                "status": "already_empty",
                "folder": human_name,
                "deleted": 0,
            }

        for chunk_bytes in _chunk([u.decode("ascii") for u in uids], _BULK_UID_CHUNK):
            uid_set = ",".join(chunk_bytes)
            status, _ = conn.uid(
                "STORE", uid_set, "+FLAGS", "(\\Deleted)"
            )
            if status != "OK":
                raise Exception("empty_trash: failed to mark as deleted")
        conn.expunge()

    return {
        "status": "emptied",
        "folder": human_name,
        "deleted": len(uids),
    }


@mcp.tool()
def get_unread_summary() -> dict:
    """
    Get unread and total message counts across ALL selectable folders.

    Iterates LIST, skips \\Noselect folders, calls STATUS on each. Much
    more efficient than calling get_folder_status per folder from the
    client side because everything happens in one IMAP session.

    Returns a dict keyed by human-readable folder name, each value
    containing {messages, unseen}. Also includes a `_summary` key with
    totals.
    """
    result: dict = {}
    total_unseen = 0
    total_messages = 0
    scanned = 0

    with imap_connection() as conn:
        status, folder_data = conn.list()
        if status != "OK" or not folder_data:
            return {"_summary": {"total_unseen": 0, "folders_scanned": 0}}

        for item in folder_data:
            parsed = _parse_folder_line(item)
            if parsed is None:
                continue
            attrs, imap_name = parsed
            # Skip non-selectable folders
            if any(a.lower() == "\\noselect" for a in attrs):
                continue

            try:
                # conn.status() is the high-level imaplib method — it applies
                # its own quoting. Passing a pre-quoted name would corrupt it.
                status, data = conn.status(imap_name, "(MESSAGES UNSEEN)")
            except Exception as e:
                logger.warning("STATUS failed for %s: %s", imap_name, e)
                continue

            if status != "OK" or not data or not data[0]:
                continue

            raw = data[0]
            response = (
                raw.decode("utf-8", errors="replace")
                if isinstance(raw, (bytes, bytearray))
                else str(raw)
            )

            messages = 0
            unseen = 0
            m = re.search(r"MESSAGES\s+(\d+)", response, re.IGNORECASE)
            if m:
                messages = int(m.group(1))
            m = re.search(r"UNSEEN\s+(\d+)", response, re.IGNORECASE)
            if m:
                unseen = int(m.group(1))

            human_name = decode_folder_name(imap_name)
            result[human_name] = {
                "messages": messages,
                "unseen": unseen,
            }
            total_unseen += unseen
            total_messages += messages
            scanned += 1

    result["_summary"] = {
        "total_unseen": total_unseen,
        "total_messages": total_messages,
        "folders_scanned": scanned,
    }
    return result


def main() -> None:
    """Entry point for the console_scripts `yandex-mail-mcp` command."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

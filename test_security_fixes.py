"""
Offline regression tests for the security fixes (P0).

These tests never touch the network: IMAP connections are stubbed, and email
fixtures are synthetic. They cover the five confirmed audit findings:

  F-1  TLS: both connections must use a verifying SSL context
  F-2  download_attachment: writes confined to save_dir, symlinks not followed
  F-3  delete_email: failed Trash copy must NOT escalate to permanent deletion
  F-4  unscoped EXPUNGE: other pending-deleted messages must not be swept
  F-5  read_email: attacker-chosen body charset must not crash the tool

Note: since fix/correctness-p1 the tools operate on UIDs (see
test_correctness_fixes.py); the stub therefore implements conn.uid(...).
"""

import email.mime.multipart
import email.mime.text
import ssl
from contextlib import contextmanager
from email.mime.application import MIMEApplication

import pytest

import server


# ---------------------------------------------------------------------------
# Stub IMAP infrastructure
# ---------------------------------------------------------------------------

class FakeIMAP:
    """Minimal imaplib-shaped stub implementing only what the tools call.

    Tools use UID commands (uid SEARCH/FETCH/COPY/STORE) and plain select/expunge.
    """

    def __init__(self, copy_status="OK", deleted_ids=None, messages=None):
        self.copy_status = copy_status
        self.deleted_ids = list(deleted_ids or [])
        self.messages = dict(messages or {})
        self.expunged = False
        self.selected = None
        self.readonly = None

    def select(self, folder, readonly=False):
        self.selected = folder
        self.readonly = readonly
        return ("OK", [b"1 EXISTS"])

    def expunge(self):
        self.expunged = True
        return ("OK", [b"1"])

    def uid(self, command, *args):
        cmd = command.upper()
        if cmd == "SEARCH":
            criteria = args[1:] if args and args[0] == "CHARSET" else args
            assert criteria == ("DELETED",), f"unexpected search: {criteria}"
            return ("OK", [" ".join(self.deleted_ids).encode()])
        if cmd == "COPY":
            self.copied = (args[0], args[1])
            return (self.copy_status, None)
        if cmd == "STORE":
            self.stored = (args[0], args[1], args[2])
            return ("OK", None)
        if cmd == "FETCH":
            uid_arg = args[0]
            if isinstance(uid_arg, (bytes, bytearray)):
                uid_arg = uid_arg.decode()
            raw = self.messages.get(uid_arg)
            return ("OK", [(None, raw)] if raw is not None else ("OK", [(None, b"")]))
        raise AssertionError(f"unexpected uid command: {cmd}")


def make_stubbed_connection(conn):
    @contextmanager
    def fake_imap_connection():
        yield conn
    return fake_imap_connection


def make_attachment_email(filename: str, payload: bytes = b"attacker-payload") -> bytes:
    msg = email.mime.multipart.MIMEMultipart()
    msg.attach(email.mime.text.MIMEText("hello", "plain", "utf-8"))
    att = MIMEApplication(payload, _subtype="octet-stream")
    att.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(att)
    return msg.as_bytes()


def make_body_email(charset: str, body: str = "body text") -> bytes:
    raw = (
        "From: attacker@example.test\r\n"
        f"Content-Type: text/plain; charset=\"{charset}\"\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()
    return raw


# ---------------------------------------------------------------------------
# F-1: TLS verification
# ---------------------------------------------------------------------------

class TestTlsContext:
    def test_context_verifies_certificate_and_hostname(self):
        assert server._TLS_CONTEXT.verify_mode == ssl.CERT_REQUIRED
        assert server._TLS_CONTEXT.check_hostname is True

    def test_context_is_not_the_unverified_stdlib_default(self):
        unverified = ssl._create_stdlib_context()
        assert server._TLS_CONTEXT.verify_mode != unverified.verify_mode
        assert server._TLS_CONTEXT.check_hostname != unverified.check_hostname


# ---------------------------------------------------------------------------
# F-2: download_attachment containment
# ---------------------------------------------------------------------------

def test_download_normalizes_traversal_name_into_save_dir(monkeypatch, tmp_path):
    save_dir = tmp_path / "downloads"
    save_dir.mkdir()
    (tmp_path / "victim.txt").write_text("ORIGINAL-CONTENT")

    conn = FakeIMAP(messages={"1": make_attachment_email("../victim.txt")})
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.download_attachment("INBOX", "1", "../victim.txt", str(save_dir))

    # The write landed inside the declared save dir (name reduced to a basename)
    assert result["status"] == "downloaded"
    written = save_dir / "victim.txt"
    assert written.read_bytes() == b"attacker-payload"
    # The file outside the save dir is untouched
    assert (tmp_path / "victim.txt").read_text() == "ORIGINAL-CONTENT"


def test_download_traversal_via_symlink_is_rejected(monkeypatch, tmp_path):
    save_dir = tmp_path / "downloads"
    save_dir.mkdir()
    outside = tmp_path / "target.txt"
    outside.write_text("ORIGINAL-CONTENT")
    (save_dir / "link.txt").symlink_to(outside)

    conn = FakeIMAP(messages={"1": make_attachment_email("link.txt")})
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    with pytest.raises(Exception):
        server.download_attachment("INBOX", "1", "link.txt", str(save_dir))
    # Symlink target is untouched
    assert outside.read_text() == "ORIGINAL-CONTENT"


def test_download_dotfile_names_are_rejected(monkeypatch, tmp_path):
    save_dir = tmp_path / "downloads"
    save_dir.mkdir()

    conn = FakeIMAP(messages={"1": make_attachment_email(".bashrc")})
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    with pytest.raises(ValueError):
        server.download_attachment("INBOX", "1", ".bashrc", str(save_dir))


def test_download_plain_name_still_works(monkeypatch, tmp_path):
    save_dir = tmp_path / "downloads"
    save_dir.mkdir()

    conn = FakeIMAP(messages={"1": make_attachment_email("report.pdf", b"%PDF-1.4")})
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.download_attachment("INBOX", "1", "report.pdf", str(save_dir))
    assert result["status"] == "downloaded"
    assert (save_dir / "report.pdf").read_bytes() == b"%PDF-1.4"


# ---------------------------------------------------------------------------
# F-3: delete_email must not escalate a failed Trash copy to permanent deletion
# ---------------------------------------------------------------------------

def test_delete_without_trash_folder_raises_and_keeps_message(monkeypatch):
    conn = FakeIMAP(copy_status="NO", deleted_ids=[])
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    with pytest.raises(Exception, match="Could not move email to Trash"):
        server.delete_email("INBOX", "1")
    assert conn.expunged is False
    assert conn.copied == ("1", "Trash")


def test_delete_success_path_expunges_only_requested_message(monkeypatch):
    conn = FakeIMAP(copy_status="OK", deleted_ids=["1"])
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.delete_email("INBOX", "1")
    assert result["status"] == "moved_to_trash"
    assert conn.expunged is True


def test_delete_skips_expunge_when_other_messages_pending(monkeypatch):
    conn = FakeIMAP(copy_status="OK", deleted_ids=["1", "2"])
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.delete_email("INBOX", "1")
    assert result["status"] == "moved_to_trash_flag_not_expunged"
    assert conn.expunged is False


# ---------------------------------------------------------------------------
# F-4: move_email unscoped EXPUNGE
# ---------------------------------------------------------------------------

def test_move_skips_expunge_when_other_messages_pending(monkeypatch):
    conn = FakeIMAP(deleted_ids=["1", "2"])
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.move_email("INBOX", "1", "Archive")
    assert result["status"] == "moved_copy_created_flag_not_expunged"
    assert conn.expunged is False


def test_move_expunges_when_only_requested_message_flagged(monkeypatch):
    conn = FakeIMAP(deleted_ids=["1"])
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.move_email("INBOX", "1", "Archive")
    assert result["status"] == "moved"
    assert conn.expunged is True


# ---------------------------------------------------------------------------
# F-5: read_email must survive attacker-chosen body charsets
# ---------------------------------------------------------------------------

def test_decode_body_falls_back_on_unknown_charset():
    text = server.decode_body(b"hello", "x-nonsense-codec")
    assert text == "hello"


def test_decode_body_honors_valid_charset():
    text = server.decode_body("привет".encode("koi8-r"), "koi8-r")
    assert text == "привет"


def test_decode_body_handles_none_payload():
    assert server.decode_body(None, "utf-8") == ""


def test_read_email_survives_unknown_body_charset(monkeypatch):
    conn = FakeIMAP(messages={"1": make_body_email("x-nonsense-codec")})
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.read_email("INBOX", "1")
    # The tool must not crash on the unknown charset; the body decodes via fallback
    assert result["body_text"].strip() == "body text"

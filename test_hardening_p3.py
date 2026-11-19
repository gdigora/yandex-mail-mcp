"""
Offline tests for the P3 hardening: invocation logging with redaction and
transient-failure retry.
"""

import imaplib
import logging
import smtplib

import pytest

import server


class TestRetry:
    def test_transient_abort_is_retried_once(self, monkeypatch):
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise imaplib.IMAP4.abort(b"socket error: EOF")
            return "recovered"

        wrapped = server.tool_with_resilience(flaky)
        assert wrapped() == "recovered"
        assert len(calls) == 2

    def test_live_observed_transient_error_is_retried(self, monkeypatch):
        # Exact error observed on a real mailbox
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise Exception("command: EXAMINE => problems with connection")
            return "recovered"

        wrapped = server.tool_with_resilience(flaky)
        assert wrapped() == "recovered"
        assert len(calls) == 2

    def test_smtp_disconnect_is_retried(self, monkeypatch):
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            return "sent"

        wrapped = server.tool_with_resilience(flaky)
        assert wrapped() == "sent"
        assert len(calls) == 2

    def test_non_transient_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        calls = []

        def broken():
            calls.append(1)
            raise ValueError("unsafe attachment filename")

        wrapped = server.tool_with_resilience(broken)
        with pytest.raises(ValueError):
            wrapped()
        assert len(calls) == 1

    def test_second_transient_failure_propagates(self, monkeypatch):
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        calls = []

        def always_flaky():
            calls.append(1)
            raise imaplib.IMAP4.abort(b"connection dropped")

        wrapped = server.tool_with_resilience(always_flaky)
        with pytest.raises(imaplib.IMAP4.abort):
            wrapped()
        assert len(calls) == 2


class TestInvocationLogging:
    def test_invocation_logged_with_sanitized_args(self, monkeypatch, caplog):
        monkeypatch.setattr(server.time, "sleep", lambda s: None)

        def read_email(folder, email_id):
            return {}

        with caplog.at_level(logging.INFO, logger="server"):
            server.tool_with_resilience(read_email)("INBOX", "42")

        assert any("tool=read_email" in r.message or "tool=read_email" in r.getMessage()
                   for r in caplog.records)
        assert any("INBOX" in r.getMessage() and "42" in r.getMessage()
                   for r in caplog.records)

    def test_body_is_never_written_to_log(self, monkeypatch, caplog):
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        secret_body = "SECRET-INJECTION-PAYLOAD-9f2b"

        def send_email(to, subject, body):
            return {}

        with caplog.at_level(logging.INFO, logger="server"):
            server.tool_with_resilience(send_email)(
                to="x@example.test", subject="hi", body=secret_body)

        log_text = caplog.text
        assert secret_body not in log_text
        assert "chars omitted" in log_text

    def test_long_args_truncated(self, monkeypatch, caplog):
        monkeypatch.setattr(server.time, "sleep", lambda s: None)

        def search_emails(folder, query, limit):
            return []

        with caplog.at_level(logging.INFO, logger="server"):
            server.tool_with_resilience(search_emails)(
                folder="I" * 500, query="ALL", limit=2)

        assert "…" in caplog.text


# ---------------------------------------------------------------------------
# Output-schema conformance: every return path must satisfy its TypedDict,
# otherwise FastMCP rejects the result AFTER the side effect has happened
# (the SendResult cc/bcc bug class).
# ---------------------------------------------------------------------------

import typing
from pydantic import TypeAdapter

_ADAPTERS = {
    name: TypeAdapter(getattr(server, name))
    for name in ("FolderInfo", "EmailSummary", "EmailFull", "SendResult",
                 "DownloadResult", "MoveResult", "DeleteResult")
}


class TestOutputSchemaConformance:
    def test_all_tools_are_resilience_wrapped(self):
        # regression: the scripted patch left read/move/delete unwrapped
        for tool in ("list_folders", "search_emails", "read_email",
                     "download_attachment", "send_email", "move_email", "delete_email"):
            assert hasattr(getattr(server, tool), "__wrapped__"), f"{tool} misses resilience"

    def test_delete_email_all_return_paths_match_schema(self, monkeypatch):
        from test_security_fixes import make_stubbed_connection
        adapter = _ADAPTERS["DeleteResult"]

        # MOVE path
        conn = FakeIMAPForSchema()
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
        r = server.delete_email("INBOX", "1")
        adapter.validate_python(r)

        # fallback UID EXPUNGE path (no MOVE, UIDPLUS present)
        conn = FakeIMAPForSchema(capabilities=("UIDPLUS",))
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
        r = server.delete_email("INBOX", "1")
        adapter.validate_python(r)

        # last-resort expunge path (no MOVE, no UIDPLUS, only this message flagged)
        conn = FakeIMAPForSchema(capabilities=())
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
        r = server.delete_email("INBOX", "1")
        adapter.validate_python(r)

        # flag_not_expunged path (another message is pending deletion)
        conn = FakeIMAPForSchema(capabilities=(), deleted_ids=["1", "2"])
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
        r = server.delete_email("INBOX", "1")
        adapter.validate_python(r)

    def test_move_email_all_return_paths_match_schema(self, monkeypatch):
        from test_security_fixes import make_stubbed_connection
        adapter = _ADAPTERS["MoveResult"]

        conn = FakeIMAPForSchema()
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
        adapter.validate_python(server.move_email("INBOX", "1", "Archive"))

        conn = FakeIMAPForSchema(capabilities=(), deleted_ids=["1", "2"])
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
        adapter.validate_python(server.move_email("INBOX", "1", "Archive"))

        conn = FakeIMAPForSchema(capabilities=(), deleted_ids=["1"])
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
        adapter.validate_python(server.move_email("INBOX", "1", "Archive"))

    def test_send_email_without_cc_bcc_matches_schema(self, monkeypatch):
        adapter = _ADAPTERS["SendResult"]
        adapter.validate_python({"status": "sent", "to": "x@y.z", "subject": "s",
                                 "cc": None, "bcc": None})


class FakeIMAPForSchema:
    """Minimal connection stub for schema-conformance tests (Yandex-like caps)."""

    def __init__(self, capabilities=("UIDPLUS", "MOVE"), deleted_ids=None):
        self.capabilities = tuple(capabilities)
        self.deleted_ids = list(deleted_ids or [])

    def select(self, folder, readonly=False):
        return ("OK", [b"1 EXISTS"])

    def list(self):
        return ("OK", [b'(\\HasNoChildren \\Trash) NIL "Trash"'])

    def expunge(self):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        cmd = command.upper()
        if cmd == "SEARCH":
            return ("OK", [" ".join(self.deleted_ids).encode()])
        return ("OK", None)

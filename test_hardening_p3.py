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

"""Tests for send_email's save_copy parameter (save copy into Sent folder)."""

import logging
from contextlib import contextmanager

import pytest

import server


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg, from_addr, recipients):
        self.sent.append((msg, from_addr, recipients))


class FakeIMAPSent:
    def __init__(self, flags="\\Sent", append_ok=True):
        self.flags = flags
        self.append_ok = append_ok
        self.appended = []

    def list(self):
        return ("OK", [f'(\\HasNoChildren {self.flags}) NIL "Sent"'.encode()])

    def append(self, folder, flags, date, data):
        self.appended.append((folder, flags, data))
        return ("OK" if self.append_ok else "NO", None)


@contextmanager
def fake_smtp():
    conn = FakeSMTP()
    yield conn


def stub_imap(conn):
    @contextmanager
    def fake_imap_connection():
        yield conn
    return fake_imap_connection


def test_copy_saved_by_default(monkeypatch):
    sent_imap = FakeIMAPSent()
    monkeypatch.setattr(server, "smtp_connection", fake_smtp)
    monkeypatch.setattr(server, "imap_connection", stub_imap(sent_imap))

    result = server.send_email("to@example.test", "hi", "body")

    assert result["status"] == "sent"
    assert result["sent_copy"] == "Sent"
    assert len(sent_imap.appended) == 1
    assert sent_imap.appended[0][0] == "Sent"
    assert "\\Seen" in sent_imap.appended[0][1]


def test_copy_not_saved_when_disabled(monkeypatch):
    sent_imap = FakeIMAPSent()
    monkeypatch.setattr(server, "smtp_connection", fake_smtp)
    monkeypatch.setattr(server, "imap_connection", stub_imap(sent_imap))

    result = server.send_email("to@example.test", "hi", "body", save_copy=False)

    assert result["status"] == "sent"
    assert result["sent_copy"] is None
    assert sent_imap.appended == []


def test_append_error_does_not_break_send(monkeypatch, caplog):
    sent_imap = FakeIMAPSent(append_ok=False)
    monkeypatch.setattr(server, "smtp_connection", fake_smtp)
    monkeypatch.setattr(server, "imap_connection", stub_imap(sent_imap))

    with caplog.at_level(logging.ERROR, logger="server"):
        result = server.send_email("to@example.test", "hi", "body")

    assert result["status"] == "sent"
    assert result["sent_copy"] is None
    assert any("Failed to save sent copy" in r.getMessage() for r in caplog.records)

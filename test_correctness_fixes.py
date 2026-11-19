"""
Offline regression tests for the correctness fixes (P1).

Covers:
  C-1  UID-based operations: ids are UIDs (stable across sessions, unaffected
       by sequence renumbering after another client's EXPUNGE)
  C-2  Multi-word search phrases: SUBJECT/FROM/BODY/TEXT values may contain
       spaces; quotes escaped; literal-continuation syntax rejected
  C-3  Robust mailbox LIST parsing: NIL delimiter, escaped quotes, UTF-7 names
  C-4  Status checks: failed STORE raises; non-numeric email_id rejected
"""

from contextlib import contextmanager

import email.mime.multipart
import email.mime.text
import pytest

import server
from test_security_fixes import FakeIMAP, make_body_email, make_stubbed_connection


# ---------------------------------------------------------------------------
# C-2: build_imap_search_criteria
# ---------------------------------------------------------------------------

class TestPhraseSearch:
    def test_multi_word_subject_is_one_quoted_value(self):
        assert server.build_imap_search_criteria("SUBJECT важный отчет") == [
            "SUBJECT", '"важный отчет"',
        ]

    def test_combined_query_keeps_unquoted_keys_intact(self):
        assert server.build_imap_search_criteria("UNSEEN FROM boss@company.com SINCE 01-Dec-2024") == [
            "UNSEEN", "FROM", '"boss@company.com"', "SINCE", "01-Dec-2024",
        ]

    def test_quoted_value_wrapped_and_embedded_quotes_escaped(self):
        assert server.build_imap_search_criteria('SUBJECT "hello world"') == [
            "SUBJECT", '"hello world"',
        ]
        assert server.build_imap_search_criteria('SUBJECT he said "hi"') == [
            "SUBJECT", '"he said \\"hi\\""',
        ]

    def test_literal_continuation_syntax_rejected(self):
        with pytest.raises(ValueError):
            server.build_imap_search_criteria("SUBJECT {2} UNSEEN")

    def test_missing_value_rejected(self):
        with pytest.raises(ValueError, match="Missing value"):
            server.build_imap_search_criteria("SUBJECT")

    def test_all_query_unchanged(self):
        assert server.build_imap_search_criteria("ALL") == ["ALL"]
        assert server.build_imap_search_criteria("UNSEEN") == ["UNSEEN"]


# ---------------------------------------------------------------------------
# C-3: parse_mailbox_list_item
# ---------------------------------------------------------------------------

class TestMailboxListParsing:
    def test_standard_line(self):
        assert server.parse_mailbox_list_item('(\\HasNoChildren) "/" "INBOX"') == ("INBOX", "INBOX")

    def test_nil_delimiter(self):
        assert server.parse_mailbox_list_item('(\\HasNoChildren) NIL "INBOX"') == ("INBOX", "INBOX")

    def test_escaped_quote_in_name(self):
        # RFC3501 quoted string: a name containing a quote is stored as \" 
        line = '(\\HasNoChildren) "/" "Sent\\" Items"'
        imap_name, human = server.parse_mailbox_list_item(line)
        assert imap_name == 'Sent" Items'
        assert human == 'Sent" Items'

    def test_cyrillic_name_decoded_from_utf7(self):
        # "Отправленные" in IMAP modified UTF-7
        imap_name, human = server.parse_mailbox_list_item('(\\HasNoChildren) "/" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-1"')
        assert imap_name == "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-1"
        assert "отправлен" in human.lower()

    def test_malformed_line_returns_none(self):
        assert server.parse_mailbox_list_item("* garbage") is None
        assert server.parse_mailbox_list_item("") is None

    def test_list_folders_skips_malformed_lines(self, monkeypatch):
        conn = type("FakeListConn", (), {})()
        conn.list = lambda: (
            "OK",
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b"* malformed garbage",
                b'(\\HasNoChildren) NIL "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-1"',
            ],
        )
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

        folders = server.list_folders()
        assert [f["imap_name"] for f in folders] == ["INBOX", "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-1"]


# ---------------------------------------------------------------------------
# C-1: UID-based operations
# ---------------------------------------------------------------------------

def make_email(uid_suffix: str = "hello") -> bytes:
    msg = email.mime.text.MIMEText(uid_suffix, "plain", "utf-8")
    return msg.as_bytes()


class TestUidOperations:
    def test_search_returns_uids_and_uses_uid_commands(self, monkeypatch):
        calls = []

        class SearchConn:
            def select(self, folder, readonly=False):
                assert readonly is True
                return ("OK", [b"1 EXISTS"])

            def uid(self, command, *args):
                calls.append((command, args))
                if command.upper() == "SEARCH":
                    return ("OK", [b"42 99 105"])
                if command.upper() == "FETCH":
                    return ("OK", [(None, make_email(f"body-{args[0]}"))])
                raise AssertionError(command)

        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(SearchConn()))

        emails = server.search_emails("INBOX", "ALL", 20)
        # UIDs are returned as ids, most recent (highest uid) first
        assert [e["id"] for e in emails] == ["105", "99", "42"]
        assert any(c[0].upper() == "SEARCH" for c in calls)

    def test_non_ascii_query_uses_utf8_charset_uid_search(self, monkeypatch):
        class SearchConn:
            def select(self, folder, readonly=False):
                return ("OK", [b"1 EXISTS"])

            def uid(self, command, *args):
                if command.upper() == "SEARCH":
                    assert args[0] == "CHARSET" and args[1] == "UTF-8"
                    return ("OK", [b"77"])
                if command.upper() == "FETCH":
                    return ("OK", [(None, make_email("тело"))])
                raise AssertionError(command)

        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(SearchConn()))

        emails = server.search_emails("INBOX", "SUBJECT привет", 20)
        assert [e["id"] for e in emails] == ["77"]

    def test_read_email_fetches_by_uid(self, monkeypatch):
        fetches = []

        class ReadConn:
            def select(self, folder, readonly=False):
                return ("OK", [b"1 EXISTS"])

            def uid(self, command, *args):
                if command.upper() == "FETCH":
                    fetches.append(args[0])
                    return ("OK", [(None, make_email("full body"))])
                raise AssertionError(command)

        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(ReadConn()))

        result = server.read_email("INBOX", "4242")
        assert result["id"] == "4242"
        assert "full body" in result["body_text"]
        assert fetches == ["4242"]

    def test_uids_stable_across_calls_despite_other_clients_expunge(self, monkeypatch):
        # Scenario: the model searched earlier (saw uid 42), then another client
        # expunged two older messages (sequence renumbering). A UID-based delete
        # of "42" still targets the same message, while sequence number 42 would
        # now point at a different one.
        conn = FakeIMAP(deleted_ids=["42"], messages={"42": make_email("the message")})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

        result = server.delete_email("INBOX", "42")
        assert result["status"] == "moved_to_trash"
        assert conn.expunged is True
        assert conn.copied == ("42", "Trash")

    def test_non_numeric_email_id_rejected_before_any_command(self, monkeypatch):
        conn = FakeIMAP()
        monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

        with pytest.raises(ValueError, match="numeric IMAP UID"):
            server.delete_email("INBOX", "1:*")
        with pytest.raises(ValueError, match="numeric IMAP UID"):
            server.read_email("INBOX", "")
        with pytest.raises(ValueError, match="numeric IMAP UID"):
            server.download_attachment("INBOX", "abc", "f.txt", "/tmp")
        # No mailbox operation ever ran
        assert conn.selected is None and conn.expunged is False


# ---------------------------------------------------------------------------
# C-4: status checks
# ---------------------------------------------------------------------------

class FakeStoreConn(FakeIMAP):
    def __init__(self, *a, store_status="OK", **kw):
        super().__init__(*a, **kw)
        self.store_status = store_status

    def uid(self, command, *args):
        if command.upper() == "STORE":
            self.stored = (args[0], args[1], args[2])
            return (self.store_status, None)
        return super().uid(command, *args)


def test_delete_failed_store_raises(monkeypatch):
    conn = FakeStoreConn(store_status="NO", deleted_ids=["1"])
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    with pytest.raises(Exception, match="Failed to mark original as deleted"):
        server.delete_email("INBOX", "1")
    assert conn.expunged is False


def test_move_failed_store_raises(monkeypatch):
    conn = FakeStoreConn(store_status="NO", deleted_ids=["1"])
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    with pytest.raises(Exception, match="Failed to mark original as deleted"):
        server.move_email("INBOX", "1", "Archive")
    assert conn.expunged is False


# ---------------------------------------------------------------------------
# P2: provenance markers and limit clamping
# ---------------------------------------------------------------------------

class FakeSearchConn:
    """Search-only connection returning a configurable uid list."""

    def __init__(self, uids):
        self.uids = list(uids)

    def select(self, folder, readonly=False):
        return ("OK", [b"1 EXISTS"])

    def uid(self, command, *args):
        if command.upper() == "SEARCH":
            return ("OK", [" ".join(self.uids).encode()])
        if command.upper() == "FETCH":
            uid = args[0].decode() if isinstance(args[0], bytes) else args[0]
            msg = email.mime.text.MIMEText(f"body {uid}", "plain", "utf-8")
            return ("OK", [(None, msg.as_bytes())])
        raise AssertionError(command)


def _run_search(monkeypatch, uids, **kwargs):
    conn = FakeSearchConn(uids)
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))
    return server.search_emails(**kwargs)


def test_search_summaries_carry_provenance(monkeypatch):
    emails = _run_search(monkeypatch, ["1", "2"], folder="INBOX", query="ALL", limit=10)
    assert all(e["provenance"] == "untrusted_remote" for e in emails)


def test_read_email_return_carries_provenance(monkeypatch):
    conn = FakeIMAP(messages={"1": make_body_email("utf-8")})
    monkeypatch.setattr(server, "imap_connection", make_stubbed_connection(conn))

    result = server.read_email("INBOX", "1")
    assert result["provenance"] == "untrusted_remote"


def test_nonpositive_limit_uses_default_20(monkeypatch):
    emails = _run_search(monkeypatch, [str(i) for i in range(1, 31)], folder="INBOX", query="ALL", limit=0)
    assert len(emails) == 20
    # Most recent (highest uid) first, truncated from the top of the list
    assert emails[0]["id"] == "30"


def test_limit_capped_at_500(monkeypatch):
    emails = _run_search(monkeypatch, [str(i) for i in range(1, 601)], folder="INBOX", query="ALL", limit=2000)
    assert len(emails) == 500
    assert emails[0]["id"] == "600"


def test_normal_limit_still_respected(monkeypatch):
    emails = _run_search(monkeypatch, [str(i) for i in range(1, 31)], folder="INBOX", query="ALL", limit=3)
    assert len(emails) == 3
    assert emails[0]["id"] == "30"

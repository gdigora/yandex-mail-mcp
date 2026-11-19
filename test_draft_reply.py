"""
Offline tests for the draft-first tools (create_draft, reply_email).

Both tools APPEND into the mailbox's Drafts folder (discovered via the
RFC6154 special-use flag) and never transmit anything to a recipient.
"""

import email.mime.text
from email import message_from_bytes

import pytest

import server
from test_security_fixes import FakeIMAP, make_stubbed_connection

DRAFTS_LINE = b'(\\HasNoChildren \\Drafts) NIL "Drafts"'


class FakeDraftIMAP(FakeIMAP):
    """FakeIMAP whose LIST also advertises the \\Drafts folder and which
    captures APPEND calls (UIDPLUS APPENDUID response)."""

    def __init__(self, *args, append_status="OK", append_uid=47136, **kw):
        super().__init__(*args, **kw)
        self.append_status = append_status
        self.append_uid = append_uid
        self.appends = []

    def list(self):
        return ("OK", [self.trash_line, DRAFTS_LINE])

    def append(self, mailbox, flags, date_time, message):
        self.appends.append({
            "mailbox": mailbox,
            "flags": flags,
            "message": message,
        })
        return (self.append_status, [f"[APPENDUID 123 {self.append_uid}] OK".encode()])


def make_stubbed_draft_connection(conn):
    @contextmanager
    def fake():
        yield conn
    return fake


from contextlib import contextmanager  # noqa: E402  (kept next to use)

ORIGINAL = email.mime.text.MIMEText(
    "исходный текст письма", "plain", "utf-8")
ORIGINAL["Subject"] = "Вопрос по заказу"
ORIGINAL["From"] = "Иван Иванов <ivan@example.test>"
ORIGINAL["To"] = "bardimnik@yandex.ru"
ORIGINAL["Message-ID"] = "<orig-123@example.test>"
ORIGINAL_BYTES = ORIGINAL.as_bytes()


class TestCreateDraft:
    def test_appends_to_drafts_folder_with_draft_flag(self, monkeypatch):
        conn = FakeDraftIMAP()
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        result = server.create_draft(
            to="friend@example.test", subject="привет", body="текст письма")

        assert result["status"] == "draft_created"
        assert result["draft_folder"] == "Drafts"
        assert result["uid"] == "47136"
        assert len(conn.appends) == 1
        assert conn.appends[0]["mailbox"] == "Drafts"
        assert "\\Draft" in conn.appends[0]["flags"]
        msg = message_from_bytes(conn.appends[0]["message"])
        assert msg["To"] == "friend@example.test"
        assert server.decode_mime_header(msg.get("Subject", "")) == "привет"
        assert msg["From"] == "bardimnik@yandex.ru"
        assert msg["Date"] is not None
        assert msg["Message-ID"] is not None
        assert "текст письма" in server.decode_body(msg.get_payload(decode=True), "utf-8")

    def test_nothing_is_sent_to_recipients(self, monkeypatch):
        # The tool only APPENDs locally; there is no SMTP path at all
        conn = FakeDraftIMAP()
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
        assert not hasattr(conn, "send_message")  # stub mirrors real usage: no SMTP involved
        server.create_draft(to="x@example.test", subject="s", body="b")

    def test_without_drafts_folder_fails_safely(self, monkeypatch):
        conn = FakeDraftIMAP()
        conn.trash_line = b'(\\HasNoChildren \\Trash) NIL "Trash"'
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        # simulate a LIST that advertises only Trash
        def list_no_drafts():
            return ("OK", [conn.trash_line])
        conn.list = list_no_drafts

        with pytest.raises(Exception, match="Drafts folder not found"):
            server.create_draft(to="x@example.test", subject="s", body="b")

    def test_append_failure_raises(self, monkeypatch):
        conn = FakeDraftIMAP(append_status="NO")
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        with pytest.raises(Exception, match="Failed to append draft"):
            server.create_draft(to="x@example.test", subject="s", body="b")


class TestReplyEmail:
    def test_reply_headers_derived_from_original(self, monkeypatch):
        conn = FakeDraftIMAP(messages={"42": ORIGINAL_BYTES})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        result = server.reply_email("INBOX", "42", "спасибо, разобрался")

        assert result["status"] == "draft_created"
        assert result["draft_folder"] == "Drafts"
        msg = message_from_bytes(conn.appends[0]["message"])
        # Recipient derived by code from the original, not by the model
        assert server.decode_mime_header(msg.get("To", "")) == "ivan@example.test"
        assert server.decode_mime_header(msg.get("Subject", "")) == "Re: Вопрос по заказу"
        assert msg["In-Reply-To"] == "<orig-123@example.test>"
        assert "<orig-123@example.test>" in msg["References"]
        payload_decoded = server.decode_body(msg.get_payload(decode=True), "utf-8")
        assert "спасибо, разобрался" in payload_decoded
        assert "> исходный текст письма" in payload_decoded
        # original quoted below the reply
        assert "> исходный текст письма" in payload_decoded

    def test_reply_does_not_double_re(self, monkeypatch):
        already_re = email.mime.text.MIMEText("body", "plain", "utf-8")
        already_re["Subject"] = "Re: Вопрос по заказу"
        already_re["From"] = "ivan@example.test"
        already_re["Message-ID"] = "<orig-123@example.test>"
        conn = FakeDraftIMAP(messages={"42": already_re.as_bytes()})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        server.reply_email("INBOX", "42", "ответ")
        msg = message_from_bytes(conn.appends[0]["message"])
        assert server.decode_mime_header(msg.get("Subject", "")) == "Re: Вопрос по заказу"

    def test_reply_prefers_reply_to_header(self, monkeypatch):
        with_reply_to = email.mime.text.MIMEText("body", "plain", "utf-8")
        with_reply_to["Subject"] = "s"
        with_reply_to["From"] = "display@example.test"
        with_reply_to["Reply-To"] = "real@example.test"
        with_reply_to["Message-ID"] = "<m-1@example.test>"
        conn = FakeDraftIMAP(messages={"42": with_reply_to.as_bytes()})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        server.reply_email("INBOX", "42", "ответ")
        msg = message_from_bytes(conn.appends[0]["message"])
        assert msg["To"] == "real@example.test"

    def test_reply_to_rfc2047_encoded_from(self, monkeypatch):
        # Cyrillic display names arrive as RFC2047 encoded-words on the wire;
        # parseaddr must see the DECODED header or the reply is addressed to
        # the encoded-word token instead of a real address
        encoded = email.mime.text.MIMEText("body", "plain", "utf-8")
        encoded["Subject"] = "s"
        encoded["From"] = "=?utf-8?b?0JjQstCw0L0g0JjQvtCyIDxpdmFuQGV4YW1wbGUudGVzdD4=?="
        encoded["Message-ID"] = "<m-2@example.test>"
        conn = FakeDraftIMAP(messages={"42": encoded.as_bytes()})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        server.reply_email("INBOX", "42", "ответ")
        msg = message_from_bytes(conn.appends[0]["message"])
        assert msg["To"] == "ivan@example.test"  # plain address, not the encoded word

    def test_reply_without_message_id_omits_threading_headers(self, monkeypatch):
        no_mid = email.mime.text.MIMEText("body", "plain", "utf-8")
        no_mid["Subject"] = "s"
        no_mid["From"] = "ivan@example.test"
        conn = FakeDraftIMAP(messages={"42": no_mid.as_bytes()})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        server.reply_email("INBOX", "42", "ответ")
        msg = message_from_bytes(conn.appends[0]["message"])
        assert server.decode_mime_header(msg.get("Subject", "")) == "Re: s"
        assert msg["In-Reply-To"] is None
        assert msg["References"] is None

    def test_reply_appends_draft_not_send(self, monkeypatch):
        conn = FakeDraftIMAP(messages={"42": ORIGINAL_BYTES})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
        server.reply_email("INBOX", "42", "ответ")
        assert len(conn.appends) == 1 and len(getattr(conn, "moves", []) or []) == 0


class TestDraftResultSchema:
    def test_conformance_with_uid(self):
        from pydantic import TypeAdapter
        adapter = TypeAdapter(server.DraftResult)
        adapter.validate_python({"status": "draft_created", "draft_folder": "Drafts", "uid": "47136"})

    def test_conformance_without_uid(self):
        # server without UIDPLUS: no APPENDUID response
        from pydantic import TypeAdapter
        adapter = TypeAdapter(server.DraftResult)
        adapter.validate_python({"status": "draft_created", "draft_folder": "Drafts", "uid": None})

    def test_all_nine_tools_are_resilience_wrapped(self):
        for tool in ("list_folders", "search_emails", "read_email", "download_attachment",
                     "send_email", "move_email", "delete_email", "create_draft", "reply_email"):
            assert hasattr(getattr(server, tool), "__wrapped__"), f"{tool} misses resilience"


class TestDraftHeaderSafety:
    """Remote content and model args must not reach a draft's header block:
    the human will send that draft, so injected headers would go out with it.
    (Found by a live-review probe: 'Bcc: attacker@evil.test' embedded in a
    remote Message-ID landed in the draft bytes verbatim.)"""

    def test_create_draft_rejects_crlf_in_header_args(self, monkeypatch):
        conn = FakeDraftIMAP()
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
        with pytest.raises(ValueError, match="must not contain line breaks"):
            server.create_draft(to="x@y.z", subject="s\nBcc: attacker@evil.test", body="b")
        with pytest.raises(ValueError, match="must not contain line breaks"):
            server.create_draft(to="x@y.z\n Bcc: attacker@evil.test", subject="s", body="b")
        with pytest.raises(ValueError, match="must not contain line breaks"):
            server.create_draft(to="x@y.z", subject="s", body="b", cc="a@b.c\n d@e.f")
        assert conn.appends == []  # nothing stored

    def test_reply_email_drops_injected_message_id(self, monkeypatch):
        m = email.mime.text.MIMEText("body", "plain", "utf-8")
        m["Subject"] = "s"
        m["From"] = "attacker@example.test"
        m["Message-ID"] = "<abc@example.test>\n Bcc: attacker@evil.test"
        conn = FakeDraftIMAP(messages={"42": m.as_bytes()})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        server.reply_email("INBOX", "42", "ответ")
        msg = message_from_bytes(conn.appends[0]["message"])
        assert msg["In-Reply-To"] is None
        raw = conn.appends[0]["message"].decode(errors="replace")
        assert "Bcc" not in raw and "attacker@evil.test" not in raw

    def test_reply_email_sanitizes_references(self, monkeypatch):
        m = email.mime.text.MIMEText("body", "plain", "utf-8")
        m["Subject"] = "s"
        m["From"] = "attacker@example.test"
        m["Message-ID"] = "<m1@example.test>"
        m["References"] = "<r1@example.test>\n X-Evil: yes"
        conn = FakeDraftIMAP(messages={"42": m.as_bytes()})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        server.reply_email("INBOX", "42", "ответ")
        msg = message_from_bytes(conn.appends[0]["message"])
        assert msg["References"] == "<r1@example.test> <m1@example.test>"
        raw = conn.appends[0]["message"].decode(errors="replace")
        assert "X-Evil" not in raw

    def test_reply_email_subject_line_breaks_sanitized(self, monkeypatch):
        folded = email.mime.text.MIMEText("body", "plain", "utf-8")
        folded["Subject"] = "line one\n line two"
        folded["From"] = "x@example.test"
        conn = FakeDraftIMAP(messages={"42": folded.as_bytes()})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))

        server.reply_email("INBOX", "42", "ответ")
        msg = message_from_bytes(conn.appends[0]["message"])
        raw = conn.appends[0]["message"].decode(errors="replace")
        assert "\n line two" not in raw

    def test_create_draft_stores_bcc(self, monkeypatch):
        conn = FakeDraftIMAP()
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
        server.create_draft(to="x@example.test", subject="s", body="b", bcc="secret@evil.test")
        msg = message_from_bytes(conn.appends[0]["message"])
        assert msg["Bcc"] == "secret@evil.test"  # client strips it on send

    def test_missing_email_clean_valueerror(self, monkeypatch):
        saved = server.EMAIL
        try:
            server.EMAIL = None
            conn = FakeDraftIMAP()
            monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
            with pytest.raises(ValueError, match="YANDEX_EMAIL must be set"):
                server.create_draft(to="x@y.z", subject="s", body="b")
            with pytest.raises(ValueError, match="YANDEX_EMAIL must be set"):
                server.reply_email("INBOX", "1", "b")
        finally:
            server.EMAIL = saved


class TestFetchGuards:
    """A nonexistent/vanished UID must raise a clean "not found" error
    instead of crashing on NoneType subscript (found live)."""

    def test_read_email_missing_uid_raises_cleanly(self, monkeypatch):
        conn = FakeDraftIMAP(messages={})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
        with pytest.raises(Exception, match="not found in INBOX"):
            server.read_email("INBOX", "999999")

    def test_reply_email_missing_uid_raises_cleanly(self, monkeypatch):
        conn = FakeDraftIMAP(messages={})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
        with pytest.raises(Exception, match="not found in INBOX"):
            server.reply_email("INBOX", "999999", "ответ")

    def test_download_attachment_missing_uid_raises_cleanly(self, monkeypatch):
        conn = FakeDraftIMAP(messages={})
        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(conn))
        with pytest.raises(Exception, match="not found in INBOX"):
            server.download_attachment("INBOX", "999999", "f.txt", "/tmp")

    def test_search_skips_vanished_messages(self, monkeypatch):
        class VanishingConn(FakeDraftIMAP):
            def uid(self, command, *args):
                if command.upper() == "SEARCH":
                    return ("OK", [b"1 2 3"])
                if command.upper() == "FETCH":
                    uid = args[0].decode() if isinstance(args[0], bytes) else args[0]
                    if uid == "2":  # vanished between SEARCH and FETCH
                        return ("OK", [None])
                    return ("OK", [(None, email.mime.text.MIMEText(f"body {uid}", "plain", "utf-8").as_bytes())])
                return super().uid(command, *args)

        monkeypatch.setattr(server, "imap_connection", make_stubbed_draft_connection(VanishingConn()))
        emails = server.search_emails("INBOX", "ALL", 10)
        assert [e["id"] for e in emails] == ["3", "1"]  # "2" skipped, most recent first

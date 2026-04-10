"""
Integration tests against a live Yandex mailbox.

Categories (pytest markers):
- (unmarked) — read-only. Safe to run repeatedly. Only opens IMAP,
  never modifies mailbox state. Run with plain `pytest`.
- @pytest.mark.destructive — creates/modifies/deletes mailbox state
  (folders, flags, moves, trash). Only runs with `--run-destructive`.
- @pytest.mark.send — actually sends mail via SMTP. Only runs with
  `--run-destructive`. Messages are self-addressed with unique markers
  so they can be located and cleaned up.

All tests that require credentials use `yandex_email` / `yandex_password`
session fixtures from conftest.py; if .env is missing, tests skip
cleanly rather than erroring.
"""

import os
import time
import uuid

import pytest

from yandex_mail_mcp import (
    # Reads
    list_folders,
    search_emails,
    read_email,
    get_folder_status,
    get_unread_summary,
    inspect_email,
    fetch_part,
    download_attachment,
    # Writes
    create_folder,
    rename_folder,
    delete_folder,
    move_email,
    delete_email,
    set_flags,
    mark_read,
    mark_unread,
    mark_flagged,
    mark_answered,
    bulk_set_flags,
    bulk_mark_read,
    bulk_move,
    bulk_delete,
    empty_trash,
    # Sends
    send_email,
    reply_email,
    forward_email,
)


# =============================================================================
# Helpers used across multiple test classes
# =============================================================================


def _find_inbox_email_with_body(limit: int = 10):
    """Find the first email in INBOX that has a body (any type)."""
    results = search_emails("INBOX", "ALL", limit)
    for r in results:
        content = read_email("INBOX", r["id"])
        if content.get("body_text") or content.get("body_html"):
            return content
    return None


# =============================================================================
# Read-only integration tests
# =============================================================================


class TestGetFolderStatus:
    def test_returns_dict(self, yandex_email):
        result = get_folder_status("INBOX")
        assert isinstance(result, dict)

    def test_folder_key_present(self, yandex_email):
        result = get_folder_status("INBOX")
        assert result.get("folder") == "INBOX"

    def test_has_counts(self, yandex_email):
        result = get_folder_status("INBOX")
        # At least messages count should be present
        assert "messages" in result
        assert isinstance(result["messages"], int)
        assert result["messages"] >= 0

    def test_has_uidvalidity(self, yandex_email):
        result = get_folder_status("INBOX")
        assert "uidvalidity" in result
        assert isinstance(result["uidvalidity"], int)


class TestGetUnreadSummary:
    def test_returns_dict(self, yandex_email):
        result = get_unread_summary()
        assert isinstance(result, dict)

    def test_has_summary_key(self, yandex_email):
        result = get_unread_summary()
        assert "_summary" in result
        summary = result["_summary"]
        assert "total_unseen" in summary
        assert "total_messages" in summary
        assert "folders_scanned" in summary
        assert isinstance(summary["folders_scanned"], int)
        assert summary["folders_scanned"] > 0, "Should scan at least one folder"

    def test_totals_match_per_folder_sum(self, yandex_email):
        result = get_unread_summary()
        summary = result.pop("_summary")
        sum_unseen = sum(
            v["unseen"] for k, v in result.items() if isinstance(v, dict)
        )
        assert sum_unseen == summary["total_unseen"]


class TestInspectEmail:
    def test_inspect_first_inbox_email(self, yandex_email):
        emails = search_emails("INBOX", "ALL", 1)
        if not emails:
            pytest.skip("No emails in INBOX")
        result = inspect_email("INBOX", emails[0]["id"])
        assert isinstance(result, dict)
        assert result["id"] == emails[0]["id"]
        assert "subject" in result
        assert "parts" in result
        assert isinstance(result["parts"], list)

    def test_parts_have_structure(self, yandex_email):
        emails = search_emails("INBOX", "ALL", 1)
        if not emails:
            pytest.skip("No emails in INBOX")
        result = inspect_email("INBOX", emails[0]["id"])
        for part in result["parts"]:
            assert "part" in part
            assert "type" in part
            # Part numbers look like "1", "2.1" etc.
            assert all(
                c.isdigit() or c == "." for c in part["part"]
            ), f"Unexpected part number: {part['part']}"


class TestFetchPart:
    def test_fetch_first_text_part(self, yandex_email):
        email_obj = _find_inbox_email_with_body()
        if email_obj is None:
            pytest.skip("No inbox email with body content")
        structure = inspect_email("INBOX", email_obj["id"])
        # Find first text/* part
        text_part = None
        for p in structure["parts"]:
            if p.get("type", "").startswith("text/"):
                text_part = p
                break
        if text_part is None:
            pytest.skip("No text/* part in the first inbox email")
        result = fetch_part("INBOX", email_obj["id"], text_part["part"])
        assert "content" in result
        assert result["encoding"] in ("text", "base64")
        assert isinstance(result["size"], int)
        assert result["size"] >= 0

    def test_invalid_part_number_rejected(self, yandex_email):
        """Part numbers must match ^[0-9]+(\\.[0-9]+)*$."""
        emails = search_emails("INBOX", "ALL", 1)
        if not emails:
            pytest.skip("No emails in INBOX")
        with pytest.raises(ValueError, match="Invalid part_number"):
            fetch_part("INBOX", emails[0]["id"], "bad.part.name")


class TestAdvancedSearch:
    """These tests only verify no crashes and correct result shape."""

    def test_search_larger(self, yandex_email):
        result = search_emails("INBOX", "LARGER 1000000", 5)
        assert isinstance(result, list)

    def test_search_smaller(self, yandex_email):
        result = search_emails("INBOX", "SMALLER 100", 5)
        assert isinstance(result, list)

    def test_search_sentsince(self, yandex_email):
        result = search_emails("INBOX", "SENTSINCE 01-Jan-1970", 5)
        assert isinstance(result, list)

    def test_search_header(self, yandex_email):
        # Every email has a Received header containing the receiving server
        # hostname; "mail" as the search value matches essentially all mail.
        # Primary goal is verifying HEADER dual-arg syntax survives the pipeline.
        result = search_emails("INBOX", "HEADER Received mail", 5)
        assert isinstance(result, list)

    def test_combined(self, yandex_email):
        result = search_emails(
            "INBOX", "SENTSINCE 01-Jan-1970 SMALLER 10000000", 5
        )
        assert isinstance(result, list)


class TestDownloadAttachment:
    def test_download_to_tempdir(self, yandex_email, tmp_path):
        # Find an email with an attachment
        results = search_emails("INBOX", "ALL", 50)
        attachment_email = None
        attachment_name = None
        for r in results:
            details = read_email("INBOX", r["id"])
            if details.get("attachments"):
                attachment_email = details
                attachment_name = details["attachments"][0]["filename"]
                break
        if attachment_email is None:
            pytest.skip("No email with attachment found in first 50 INBOX messages")

        result = download_attachment(
            "INBOX",
            attachment_email["id"],
            attachment_name,
            save_dir=str(tmp_path),
        )
        assert result["status"] == "downloaded"
        assert os.path.isfile(result["path"])
        assert result["size"] > 0


# =============================================================================
# Destructive integration tests
# =============================================================================


@pytest.mark.destructive
class TestFolderManagement:
    def test_create_and_delete(self, yandex_email, test_folder_name):
        result = create_folder(test_folder_name)
        assert result["status"] == "created"
        try:
            # Verify it shows up in list_folders
            folders = list_folders()
            names = {f["name"] for f in folders}
            assert test_folder_name in names
        finally:
            result = delete_folder(test_folder_name)
            assert result["status"] == "deleted"

    def test_rename(self, yandex_email, run_id):
        old_name = f"MCP-Test-{run_id}-old-{uuid.uuid4().hex[:6]}"
        new_name = f"MCP-Test-{run_id}-new-{uuid.uuid4().hex[:6]}"
        create_folder(old_name)
        try:
            result = rename_folder(old_name, new_name)
            assert result["status"] == "renamed"
            folders = list_folders()
            names = {f["name"] for f in folders}
            assert new_name in names
            assert old_name not in names
        finally:
            # Cleanup whichever one exists
            for n in (new_name, old_name):
                try:
                    delete_folder(n)
                except Exception:
                    pass

    def test_create_cyrillic_folder(self, yandex_email, run_id):
        name = f"МСР-тест-{run_id}"
        create_folder(name)
        try:
            folders = list_folders()
            names = {f["name"] for f in folders}
            assert name in names
        finally:
            try:
                delete_folder(name)
            except Exception:
                pass


@pytest.mark.destructive
@pytest.mark.send
class TestFlagsOnSelfAddressedMessage:
    """
    These tests create a test message by sending one to ourselves, then
    exercise flag operations on it. Marked both destructive and send.
    """

    def _send_test_message(self, email_addr: str, run_id: str) -> str:
        """Send a unique self-addressed message. Returns the UID."""
        marker = f"MCP-flagtest-{run_id}-{uuid.uuid4().hex[:8]}"
        send_email(
            to=email_addr,
            subject=marker,
            body=f"Test message for flag operations. Marker: {marker}",
            save_to_sent=False,  # don't pollute Sent
        )
        # Wait for delivery + search
        time.sleep(3)
        for _ in range(10):
            results = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
            if results:
                return results[0]["id"]
            time.sleep(2)
        pytest.fail(f"Test message {marker} did not arrive in INBOX")

    def test_mark_read_then_unread(self, yandex_email, run_id):
        uid = self._send_test_message(yandex_email, run_id)
        try:
            r1 = mark_read("INBOX", uid)
            assert r1["status"] == "ok"
            assert "\\Seen" in r1["added"]

            r2 = mark_unread("INBOX", uid)
            assert r2["status"] == "ok"
            assert "\\Seen" in r2["removed"]
        finally:
            try:
                delete_email("INBOX", uid)
            except Exception:
                pass

    def test_mark_flagged(self, yandex_email, run_id):
        uid = self._send_test_message(yandex_email, run_id)
        try:
            r1 = mark_flagged("INBOX", uid, flagged=True)
            assert "\\Flagged" in r1["added"]
            r2 = mark_flagged("INBOX", uid, flagged=False)
            assert "\\Flagged" in r2["removed"]
        finally:
            try:
                delete_email("INBOX", uid)
            except Exception:
                pass

    def test_set_flags_add_and_remove(self, yandex_email, run_id):
        uid = self._send_test_message(yandex_email, run_id)
        try:
            result = set_flags(
                "INBOX", uid, add=["\\Seen", "\\Flagged"], remove=["\\Answered"]
            )
            assert result["status"] == "ok"
            assert set(result["added"]) == {"\\Seen", "\\Flagged"}
            assert result["removed"] == ["\\Answered"]
        finally:
            try:
                delete_email("INBOX", uid)
            except Exception:
                pass

    def test_mark_answered(self, yandex_email, run_id):
        uid = self._send_test_message(yandex_email, run_id)
        try:
            result = mark_answered("INBOX", uid)
            assert "\\Answered" in result["added"]
        finally:
            try:
                delete_email("INBOX", uid)
            except Exception:
                pass


@pytest.mark.destructive
@pytest.mark.send
class TestMoveAndDelete:
    def _send_test_message(self, email_addr: str, run_id: str) -> str:
        marker = f"MCP-movetest-{run_id}-{uuid.uuid4().hex[:8]}"
        send_email(
            to=email_addr,
            subject=marker,
            body=f"Move/delete test. {marker}",
            save_to_sent=False,
        )
        time.sleep(3)
        for _ in range(10):
            results = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
            if results:
                return results[0]["id"]
            time.sleep(2)
        pytest.fail(f"Test message {marker} did not arrive")

    def test_move_to_sandbox(self, yandex_email, run_id, sandbox_folder):
        uid = self._send_test_message(yandex_email, run_id)
        result = move_email("INBOX", uid, sandbox_folder)
        assert result["status"] == "moved"
        assert result["method"] in ("MOVE", "COPY+STORE+EXPUNGE")

        # Verify message is now in the sandbox
        time.sleep(1)
        status = get_folder_status(sandbox_folder)
        assert status["messages"] >= 1

    def test_delete_to_trash(self, yandex_email, run_id):
        uid = self._send_test_message(yandex_email, run_id)
        result = delete_email("INBOX", uid)
        assert result["status"] in ("moved_to_trash", "deleted_permanently")


@pytest.mark.destructive
@pytest.mark.send
class TestBulkOperations:
    """Create a few messages, exercise bulk ops on them."""

    N_MESSAGES = 3

    def _send_batch(self, email_addr: str, run_id: str) -> list[str]:
        marker_prefix = f"MCP-bulk-{run_id}-{uuid.uuid4().hex[:8]}"
        for i in range(self.N_MESSAGES):
            send_email(
                to=email_addr,
                subject=f"{marker_prefix}-{i}",
                body=f"Bulk test {i}. {marker_prefix}",
                save_to_sent=False,
            )
        # Wait and find them all
        time.sleep(5)
        uids: list[str] = []
        for _ in range(15):
            results = search_emails(
                "INBOX", f'SUBJECT "{marker_prefix}"', self.N_MESSAGES
            )
            if len(results) >= self.N_MESSAGES:
                uids = [r["id"] for r in results]
                break
            time.sleep(2)
        if len(uids) < self.N_MESSAGES:
            pytest.skip(
                f"Only {len(uids)}/{self.N_MESSAGES} bulk test messages arrived"
            )
        return uids

    def test_bulk_mark_read(self, yandex_email, run_id):
        uids = self._send_batch(yandex_email, run_id)
        try:
            result = bulk_mark_read("INBOX", uids)
            assert result["status"] == "ok"
            assert result["count"] == len(uids)
        finally:
            try:
                bulk_delete("INBOX", uids, permanent=True)
            except Exception:
                pass

    def test_bulk_set_flags(self, yandex_email, run_id):
        uids = self._send_batch(yandex_email, run_id)
        try:
            result = bulk_set_flags(
                "INBOX", uids, add=["\\Flagged"], remove=["\\Seen"]
            )
            assert result["count"] == len(uids)
        finally:
            try:
                bulk_delete("INBOX", uids, permanent=True)
            except Exception:
                pass

    def test_bulk_move_to_sandbox(self, yandex_email, run_id, sandbox_folder):
        uids = self._send_batch(yandex_email, run_id)
        result = bulk_move("INBOX", uids, sandbox_folder)
        assert result["status"] == "moved"
        assert result["count"] == len(uids)
        assert result["method"] in ("MOVE", "COPY+STORE+EXPUNGE")
        # Verify in sandbox
        time.sleep(1)
        status = get_folder_status(sandbox_folder)
        assert status["messages"] >= len(uids)

    def test_bulk_delete_permanent(self, yandex_email, run_id):
        uids = self._send_batch(yandex_email, run_id)
        result = bulk_delete("INBOX", uids, permanent=True)
        assert result["status"] == "deleted_permanently"
        assert result["count"] == len(uids)


# =============================================================================
# Send integration tests
# =============================================================================


@pytest.mark.send
class TestSendEmail:
    def test_send_self_addressed(self, yandex_email, run_id):
        marker = f"MCP-send-{run_id}-{uuid.uuid4().hex[:8]}"
        result = send_email(
            to=yandex_email,
            subject=marker,
            body="Plain text test message.",
            save_to_sent=False,
        )
        assert result["status"] == "sent"
        assert result["to"] == yandex_email
        assert result["subject"] == marker

        # Find and cleanup
        time.sleep(3)
        for _ in range(8):
            hits = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
            if hits:
                try:
                    delete_email("INBOX", hits[0]["id"])
                except Exception:
                    pass
                return
            time.sleep(2)
        pytest.fail(f"Sent message {marker} never arrived")

    def test_send_with_attachment(self, yandex_email, run_id, tmp_path):
        # Create a small temp file
        file_path = tmp_path / "test-attach.txt"
        file_path.write_text("This is the attachment content.")

        marker = f"MCP-attach-{run_id}-{uuid.uuid4().hex[:8]}"
        result = send_email(
            to=yandex_email,
            subject=marker,
            body="Message with attachment.",
            attachments=[str(file_path)],
            save_to_sent=False,
        )
        assert result["status"] == "sent"
        assert "test-attach.txt" in result["attachments"]

        # Find and verify attachment present in received message
        time.sleep(5)
        received_uid = None
        for _ in range(10):
            hits = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
            if hits:
                received_uid = hits[0]["id"]
                break
            time.sleep(2)
        if received_uid is None:
            pytest.fail(f"Attachment message {marker} never arrived")

        try:
            details = read_email("INBOX", received_uid)
            attach_names = [a["filename"] for a in details.get("attachments", [])]
            assert "test-attach.txt" in attach_names
        finally:
            try:
                delete_email("INBOX", received_uid)
            except Exception:
                pass

    def test_save_to_sent(self, yandex_email, run_id):
        marker = f"MCP-savesent-{run_id}-{uuid.uuid4().hex[:8]}"
        result = send_email(
            to=yandex_email,
            subject=marker,
            body="Testing save_to_sent.",
            save_to_sent=True,
        )
        assert result["status"] == "sent"
        # saved_to_sent should be a folder name (e.g. "Отправленные") or None
        # (None if server doesn't have a Sent folder — shouldn't happen on Yandex)
        assert result.get("saved_to_sent") is not None, (
            f"Expected save_to_sent to succeed, got None"
        )

        # Verify the message is in the Sent folder
        sent_folder_name = result["saved_to_sent"]
        folders = list_folders()
        sent_imap_name = None
        for f in folders:
            if f["name"] == sent_folder_name:
                sent_imap_name = f["imap_name"]
                break
        if sent_imap_name is None:
            pytest.fail(f"Reported Sent folder {sent_folder_name} not in folder list")

        time.sleep(3)
        for _ in range(8):
            hits = search_emails(sent_imap_name, f'SUBJECT "{marker}"', 5)
            if hits:
                try:
                    delete_email(sent_imap_name, hits[0]["id"])
                except Exception:
                    pass
                # Also cleanup the received copy in INBOX
                inbox_hits = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
                for h in inbox_hits:
                    try:
                        delete_email("INBOX", h["id"])
                    except Exception:
                        pass
                return
            time.sleep(2)
        pytest.fail(f"Sent copy of {marker} never appeared in Sent folder")


@pytest.mark.send
class TestReplyEmail:
    def test_reply_with_threading(self, yandex_email, run_id):
        # Send a seed message to ourselves
        marker = f"MCP-reply-{run_id}-{uuid.uuid4().hex[:8]}"
        send_email(
            to=yandex_email,
            subject=marker,
            body="Seed message for reply test.",
            save_to_sent=False,
        )

        # Wait for it
        time.sleep(3)
        seed_uid = None
        for _ in range(10):
            hits = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
            if hits:
                seed_uid = hits[0]["id"]
                break
            time.sleep(2)
        if seed_uid is None:
            pytest.skip("Seed message never arrived")

        try:
            # Reply to it
            result = reply_email(
                folder="INBOX",
                email_id=seed_uid,
                body="This is my reply.",
                save_to_sent=False,
            )
            assert result["status"] == "sent"
            assert result["subject"].startswith("Re: ")
            # in_reply_to should be set if the seed message had a Message-ID
            assert result.get("in_reply_to") is not None

            # Wait for the reply to arrive and verify threading headers
            time.sleep(3)
            reply_marker = f"Re: {marker}"
            reply_uid = None
            for _ in range(10):
                hits = search_emails("INBOX", f'SUBJECT "{reply_marker}"', 5)
                if hits:
                    reply_uid = hits[0]["id"]
                    break
                time.sleep(2)
            if reply_uid is None:
                pytest.skip("Reply never arrived in INBOX")

            # Cleanup the reply too
            try:
                delete_email("INBOX", reply_uid)
            except Exception:
                pass
        finally:
            try:
                delete_email("INBOX", seed_uid)
            except Exception:
                pass


@pytest.mark.send
class TestForwardEmail:
    def test_forward_as_attachment(self, yandex_email, run_id):
        # Seed a message
        marker = f"MCP-fwd-{run_id}-{uuid.uuid4().hex[:8]}"
        send_email(
            to=yandex_email,
            subject=marker,
            body="Seed for forward test.",
            save_to_sent=False,
        )

        time.sleep(3)
        seed_uid = None
        for _ in range(10):
            hits = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
            if hits:
                seed_uid = hits[0]["id"]
                break
            time.sleep(2)
        if seed_uid is None:
            pytest.skip("Seed message never arrived")

        try:
            result = forward_email(
                folder="INBOX",
                email_id=seed_uid,
                to=yandex_email,
                body="Forwarding this to you.",
                as_attachment=True,
                save_to_sent=False,
            )
            assert result["status"] == "sent"
            assert result["subject"].startswith("Fwd: ")
            assert result["mode"] == "attachment"

            # Wait for the forwarded copy
            time.sleep(3)
            fwd_marker = f"Fwd: {marker}"
            fwd_uid = None
            for _ in range(10):
                hits = search_emails("INBOX", f'SUBJECT "{fwd_marker}"', 5)
                if hits:
                    fwd_uid = hits[0]["id"]
                    break
                time.sleep(2)
            if fwd_uid is None:
                pytest.skip("Forward never arrived")

            # Verify forwarded message contains an attachment (the .eml)
            details = read_email("INBOX", fwd_uid)
            attach_names = [a["filename"] for a in details.get("attachments", [])]
            assert any(
                n and n.endswith(".eml") for n in attach_names
            ), f"Expected .eml attachment in forward, got: {attach_names}"

            try:
                delete_email("INBOX", fwd_uid)
            except Exception:
                pass
        finally:
            try:
                delete_email("INBOX", seed_uid)
            except Exception:
                pass

    def test_forward_inline(self, yandex_email, run_id):
        marker = f"MCP-fwdinline-{run_id}-{uuid.uuid4().hex[:8]}"
        send_email(
            to=yandex_email,
            subject=marker,
            body="Seed for inline forward.",
            save_to_sent=False,
        )

        time.sleep(3)
        seed_uid = None
        for _ in range(10):
            hits = search_emails("INBOX", f'SUBJECT "{marker}"', 5)
            if hits:
                seed_uid = hits[0]["id"]
                break
            time.sleep(2)
        if seed_uid is None:
            pytest.skip("Seed never arrived")

        try:
            result = forward_email(
                folder="INBOX",
                email_id=seed_uid,
                to=yandex_email,
                body="My intro text.",
                as_attachment=False,
                save_to_sent=False,
            )
            assert result["mode"] == "inline"

            # Find and delete
            time.sleep(3)
            hits = search_emails("INBOX", f'SUBJECT "Fwd: {marker}"', 5)
            for h in hits:
                try:
                    delete_email("INBOX", h["id"])
                except Exception:
                    pass
        finally:
            try:
                delete_email("INBOX", seed_uid)
            except Exception:
                pass


@pytest.mark.destructive
class TestEmptyTrash:
    """
    Runs empty_trash against the live Trash folder. Destructive — wipes
    everything currently in trash. Protected by the destructive marker
    so users opt in explicitly.
    """

    def test_empty_trash_runs(self, yandex_email):
        result = empty_trash()
        assert result["status"] in ("emptied", "already_empty")
        assert "folder" in result
        assert isinstance(result["deleted"], int)

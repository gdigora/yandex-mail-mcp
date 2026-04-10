"""
Behavioral tests for Yandex Mail MCP Server.

These are integration tests that run against a live Yandex mailbox.
Requires valid credentials in .env file.
"""

import pytest
from yandex_mail_mcp import (
    list_folders,
    search_emails,
    read_email,
    build_imap_search_criteria,
)


class TestListFolders:
    """Tests for list_folders() function."""

    def test_returns_list(self):
        """list_folders should return a list."""
        result = list_folders()
        assert isinstance(result, list)

    def test_folders_have_required_fields(self):
        """Each folder should have 'name' and 'imap_name' fields."""
        result = list_folders()
        assert len(result) > 0, "Should have at least one folder"

        for folder in result:
            assert "name" in folder, "Folder should have 'name' field"
            assert "imap_name" in folder, "Folder should have 'imap_name' field"

    def test_folder_names_are_decoded(self):
        """Folder names should be human-readable (decoded from IMAP UTF-7)."""
        result = list_folders()

        # At least one folder name should not start with '&' (decoded)
        decoded_names = [f["name"] for f in result if not f["name"].startswith("&")]
        assert len(decoded_names) > 0, "At least some folder names should be decoded"

    def test_inbox_is_accessible(self):
        """INBOX should be accessible (even if not in folder list)."""
        # INBOX always exists in IMAP, even if not returned by LIST
        result = search_emails("INBOX", "ALL", 1)
        assert isinstance(result, list)


class TestSearchEmails:
    """Tests for search_emails() function."""

    def test_search_all_returns_list(self):
        """search_emails with ALL query should return a list."""
        result = search_emails("INBOX", "ALL", 5)
        assert isinstance(result, list)

    def test_search_results_have_required_fields(self):
        """Each email result should have id, subject, from, date fields."""
        result = search_emails("INBOX", "ALL", 5)

        if len(result) > 0:
            email = result[0]
            assert "id" in email, "Email should have 'id' field"
            assert "subject" in email, "Email should have 'subject' field"
            assert "from" in email, "Email should have 'from' field"
            assert "date" in email, "Email should have 'date' field"

    def test_search_respects_limit(self):
        """search_emails should respect the limit parameter."""
        result = search_emails("INBOX", "ALL", 3)
        assert len(result) <= 3

    def test_search_by_from_address(self):
        """search_emails should work with FROM query."""
        # This test verifies the query doesn't cause syntax error
        result = search_emails("INBOX", "FROM test@example.com", 5)
        assert isinstance(result, list)

    def test_search_by_subject(self):
        """search_emails should work with SUBJECT query."""
        result = search_emails("INBOX", "SUBJECT test", 5)
        assert isinstance(result, list)

    def test_search_unseen(self):
        """search_emails should work with UNSEEN query."""
        result = search_emails("INBOX", "UNSEEN", 5)
        assert isinstance(result, list)


class TestReadEmail:
    """Tests for read_email() function."""

    def test_read_email_returns_dict(self):
        """read_email should return a dictionary with email content."""
        # First, get an email ID from inbox
        emails = search_emails("INBOX", "ALL", 1)
        if len(emails) == 0:
            pytest.skip("No emails in inbox to test")

        email_id = emails[0]["id"]
        result = read_email("INBOX", email_id)
        assert isinstance(result, dict)

    def test_read_email_has_required_fields(self):
        """read_email result should have all required fields."""
        emails = search_emails("INBOX", "ALL", 1)
        if len(emails) == 0:
            pytest.skip("No emails in inbox to test")

        email_id = emails[0]["id"]
        result = read_email("INBOX", email_id)

        required_fields = ["id", "subject", "from", "to", "date", "body_text", "body_html", "attachments"]
        for field in required_fields:
            assert field in result, f"Email should have '{field}' field"

    def test_read_email_has_body_content(self):
        """read_email should return body content (text or html)."""
        emails = search_emails("INBOX", "ALL", 1)
        if len(emails) == 0:
            pytest.skip("No emails in inbox to test")

        email_id = emails[0]["id"]
        result = read_email("INBOX", email_id)

        # At least one body type should have content
        has_body = bool(result.get("body_text")) or bool(result.get("body_html"))
        assert has_body, "Email should have body_text or body_html"


class TestBuildImapSearchCriteria:
    """Tests for the IMAP search criteria builder helper."""

    def test_all_query(self):
        """ALL query should return ['ALL']."""
        assert build_imap_search_criteria("ALL") == ["ALL"]
        assert build_imap_search_criteria("all") == ["ALL"]

    def test_from_query_is_quoted(self):
        """FROM queries should have the email address quoted."""
        result = build_imap_search_criteria("FROM test@example.com")
        assert result == ["FROM", '"test@example.com"']

    def test_to_query_is_quoted(self):
        """TO queries should have the email address quoted."""
        result = build_imap_search_criteria("TO recipient@test.com")
        assert result == ["TO", '"recipient@test.com"']

    def test_subject_query_is_quoted(self):
        """SUBJECT queries should have the search term quoted."""
        result = build_imap_search_criteria("SUBJECT hello")
        assert result == ["SUBJECT", '"hello"']

    def test_combined_query(self):
        """Combined queries should handle multiple keywords."""
        result = build_imap_search_criteria("UNSEEN FROM boss@company.com")
        assert result == ["UNSEEN", "FROM", '"boss@company.com"']

    def test_since_query_not_quoted(self):
        """SINCE/BEFORE queries should not quote dates."""
        result = build_imap_search_criteria("SINCE 01-Dec-2024")
        assert result == ["SINCE", "01-Dec-2024"]


class TestSearchInCustomFolder:
    """Tests for searching in Russian-named folders."""

    def test_search_in_sent_folder(self):
        """Should be able to search in Sent folder (Отправленные)."""
        folders = list_folders()

        # Find sent folder
        sent_folder = None
        for f in folders:
            if "отправлен" in f["name"].lower() or "sent" in f["name"].lower():
                sent_folder = f["imap_name"]
                break

        if sent_folder is None:
            pytest.skip("Sent folder not found")

        result = search_emails(sent_folder, "ALL", 5)
        assert isinstance(result, list)

    def test_search_in_folder_by_imap_name(self):
        """Should be able to search using raw IMAP folder name."""
        folders = list_folders()

        if len(folders) > 0:
            # Use the first non-INBOX folder's imap_name
            test_folder = None
            for f in folders:
                if f["imap_name"].upper() != "INBOX":
                    test_folder = f["imap_name"]
                    break

            if test_folder:
                result = search_emails(test_folder, "ALL", 5)
                assert isinstance(result, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

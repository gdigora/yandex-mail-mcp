"""
Pure unit tests for server.py helpers.

These tests never open network connections. They import private helpers
directly and exercise edge cases that previous code reviews have caught.
No markers — always run.
"""

import pytest

from yandex_mail_mcp import (
    # Extended search parser
    build_imap_search_criteria,
    # BODYSTRUCTURE
    parse_bodystructure,
    _tokenize_bodystructure,
    _bodystructure_params_to_dict,
    # Subject prefix dedupe
    _dedupe_re_prefix,
    _dedupe_fwd_prefix,
    # References trimming
    _trim_references,
    # Bulk helpers
    _normalize_uid_list,
    _chunk,
    # Folder name helpers
    encode_folder_name,
    decode_folder_name,
    _quote_folder_for_command,
    _parse_folder_line,
    # Capability check
    _has_capability,
    # Flag manipulation
    _set_flags_impl,
)


# =============================================================================
# Search criteria parser
# =============================================================================


class TestSearchCriteriaBasicKeywords:
    """Existing behavior (regression): FROM/TO/CC/BCC/SUBJECT/BODY/TEXT quoted."""

    def test_all(self):
        assert build_imap_search_criteria("ALL") == ["ALL"]

    def test_all_lowercase(self):
        assert build_imap_search_criteria("all") == ["ALL"]

    def test_empty(self):
        assert build_imap_search_criteria("") == ["ALL"]

    def test_whitespace_only(self):
        assert build_imap_search_criteria("   ") == ["ALL"]

    def test_from_quoted(self):
        assert build_imap_search_criteria("FROM alice@x.com") == [
            "FROM",
            '"alice@x.com"',
        ]

    def test_to_quoted(self):
        assert build_imap_search_criteria("TO bob@x.com") == [
            "TO",
            '"bob@x.com"',
        ]

    def test_subject_quoted(self):
        assert build_imap_search_criteria("SUBJECT hello") == [
            "SUBJECT",
            '"hello"',
        ]

    def test_body_quoted(self):
        assert build_imap_search_criteria("BODY contract") == [
            "BODY",
            '"contract"',
        ]

    def test_text_quoted(self):
        assert build_imap_search_criteria("TEXT invoice") == [
            "TEXT",
            '"invoice"',
        ]

    def test_existing_quotes_normalized(self):
        """Values already wrapped in quotes are re-quoted, not double-quoted."""
        assert build_imap_search_criteria('FROM "alice@x.com"') == [
            "FROM",
            '"alice@x.com"',
        ]

    def test_single_quotes_normalized(self):
        assert build_imap_search_criteria("FROM 'alice@x.com'") == [
            "FROM",
            '"alice@x.com"',
        ]


class TestSearchCriteriaAtomKeywords:
    """SINCE/BEFORE/ON/SENT*, LARGER/SMALLER, KEYWORD/UNKEYWORD not quoted."""

    def test_since(self):
        assert build_imap_search_criteria("SINCE 01-Dec-2024") == [
            "SINCE",
            "01-Dec-2024",
        ]

    def test_before(self):
        assert build_imap_search_criteria("BEFORE 31-Dec-2024") == [
            "BEFORE",
            "31-Dec-2024",
        ]

    def test_on(self):
        assert build_imap_search_criteria("ON 15-Jan-2024") == [
            "ON",
            "15-Jan-2024",
        ]

    def test_sentsince(self):
        assert build_imap_search_criteria("SENTSINCE 01-Jan-2024") == [
            "SENTSINCE",
            "01-Jan-2024",
        ]

    def test_sentbefore(self):
        assert build_imap_search_criteria("SENTBEFORE 01-Feb-2024") == [
            "SENTBEFORE",
            "01-Feb-2024",
        ]

    def test_senton(self):
        assert build_imap_search_criteria("SENTON 15-Mar-2024") == [
            "SENTON",
            "15-Mar-2024",
        ]

    def test_larger(self):
        assert build_imap_search_criteria("LARGER 1048576") == [
            "LARGER",
            "1048576",
        ]

    def test_smaller(self):
        assert build_imap_search_criteria("SMALLER 1024") == [
            "SMALLER",
            "1024",
        ]

    def test_keyword(self):
        assert build_imap_search_criteria("KEYWORD Important") == [
            "KEYWORD",
            "Important",
        ]

    def test_unkeyword(self):
        assert build_imap_search_criteria("UNKEYWORD Old") == [
            "UNKEYWORD",
            "Old",
        ]


class TestSearchCriteriaHeader:
    """HEADER is a dual-arg keyword: field (atom) + value (quoted)."""

    def test_header_simple(self):
        assert build_imap_search_criteria("HEADER List-Id announce") == [
            "HEADER",
            "List-Id",
            '"announce"',
        ]

    def test_header_with_quoted_value(self):
        assert build_imap_search_criteria(
            'HEADER X-Custom "some value"'
        ) == [
            "HEADER",
            "X-Custom",
            '"some value"',
        ]

    def test_header_insufficient_args(self):
        """HEADER with only 1 or 0 following tokens — pass-through as atom."""
        # Only field name, no value — falls through to pass-through
        assert build_imap_search_criteria("HEADER")[0] == "HEADER"


class TestSearchCriteriaLogical:
    """OR/NOT/parens pass through unchanged."""

    def test_not_flag(self):
        assert build_imap_search_criteria("NOT DELETED") == ["NOT", "DELETED"]

    def test_or_with_from(self):
        # OR FROM alice FROM bob — each FROM value gets quoted
        assert build_imap_search_criteria(
            "OR FROM alice@x.com FROM bob@x.com"
        ) == [
            "OR",
            "FROM",
            '"alice@x.com"',
            "FROM",
            '"bob@x.com"',
        ]

    def test_standalone_flags(self):
        assert build_imap_search_criteria("UNSEEN") == ["UNSEEN"]
        assert build_imap_search_criteria("FLAGGED") == ["FLAGGED"]
        assert build_imap_search_criteria("ANSWERED") == ["ANSWERED"]


class TestSearchCriteriaCombined:
    """Real-world queries combining multiple keyword types."""

    def test_unseen_from(self):
        assert build_imap_search_criteria("UNSEEN FROM boss@company.com") == [
            "UNSEEN",
            "FROM",
            '"boss@company.com"',
        ]

    def test_large_unread_from_boss(self):
        assert build_imap_search_criteria(
            "UNSEEN LARGER 1000 FROM boss@x.com"
        ) == [
            "UNSEEN",
            "LARGER",
            "1000",
            "FROM",
            '"boss@x.com"',
        ]

    def test_subject_and_since(self):
        assert build_imap_search_criteria(
            "SUBJECT invoice SINCE 01-Jan-2024"
        ) == [
            "SUBJECT",
            '"invoice"',
            "SINCE",
            "01-Jan-2024",
        ]

    def test_header_and_flag(self):
        assert build_imap_search_criteria(
            "UNSEEN HEADER List-Id announce"
        ) == [
            "UNSEEN",
            "HEADER",
            "List-Id",
            '"announce"',
        ]


# =============================================================================
# BODYSTRUCTURE parser
# =============================================================================


class TestBodystructureTokenizer:
    """Low-level tokenizer edge cases."""

    def test_empty(self):
        assert _tokenize_bodystructure(b"") == []

    def test_simple_atom(self):
        assert _tokenize_bodystructure(b"ABC") == ["ABC"]

    def test_nil(self):
        assert _tokenize_bodystructure(b"NIL") == [None]

    def test_number(self):
        assert _tokenize_bodystructure(b"12345") == [12345]

    def test_quoted_string(self):
        assert _tokenize_bodystructure(b'"hello"') == ["hello"]

    def test_quoted_string_with_escaped_quote(self):
        """Escaped \\" inside quoted string should be preserved."""
        assert _tokenize_bodystructure(b'"a\\"b"') == ['a"b']

    def test_parens(self):
        assert _tokenize_bodystructure(b"()") == ["(", ")"]

    def test_nested(self):
        tokens = _tokenize_bodystructure(b'("a" 1 NIL)')
        assert tokens == ["(", "a", 1, None, ")"]

    def test_whitespace_handled(self):
        tokens = _tokenize_bodystructure(b'(  "a"   NIL  )')
        assert tokens == ["(", "a", None, ")"]


class TestBodystructureParser:
    """Recursive-descent parser on realistic samples."""

    def test_parse_empty(self):
        assert parse_bodystructure(b"") == []

    def test_parse_nil(self):
        assert parse_bodystructure(b"NIL") == []

    def test_single_part_text_plain(self):
        sample = (
            b'("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 1234 42 '
            b"NIL NIL NIL NIL)"
        )
        parts = parse_bodystructure(sample)
        assert len(parts) == 1
        assert parts[0]["part"] == "1"
        assert parts[0]["type"] == "text/plain"
        assert parts[0]["size"] == 1234
        assert parts[0]["charset"] == "utf-8"

    def test_single_part_text_html(self):
        sample = (
            b'("text" "html" ("charset" "utf-8") NIL NIL "quoted-printable" '
            b"8000 150 NIL NIL NIL NIL)"
        )
        parts = parse_bodystructure(sample)
        assert len(parts) == 1
        assert parts[0]["type"] == "text/html"
        assert parts[0]["size"] == 8000

    def test_multipart_alternative(self):
        sample = (
            b'(("text" "plain" ("charset" "utf-8") NIL NIL "quoted-printable" '
            b'200 5 NIL NIL NIL NIL)("text" "html" ("charset" "utf-8") NIL '
            b'NIL "quoted-printable" 1500 20 NIL NIL NIL NIL) "alternative" '
            b'("boundary" "===abc") NIL NIL NIL)'
        )
        parts = parse_bodystructure(sample)
        assert len(parts) == 2
        assert parts[0]["part"] == "1"
        assert parts[0]["type"] == "text/plain"
        assert parts[1]["part"] == "2"
        assert parts[1]["type"] == "text/html"

    def test_multipart_mixed_with_attachment(self):
        sample = (
            b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 100 3 NIL '
            b'NIL NIL NIL)("application" "pdf" ("name" "report.pdf") NIL NIL '
            b'"base64" 52428 NIL ("attachment" ("filename" "report.pdf")) NIL '
            b'NIL) "mixed" ("boundary" "===xyz") NIL NIL NIL)'
        )
        parts = parse_bodystructure(sample)
        assert len(parts) == 2
        assert parts[0]["type"] == "text/plain"
        assert parts[1]["type"] == "application/pdf"
        assert parts[1]["size"] == 52428
        assert parts[1]["filename"] == "report.pdf"
        assert parts[1]["disposition"] == "attachment"

    def test_message_rfc822_disposition_index(self):
        """
        message/rfc822 parts have extra envelope/body/lines fields which
        shift the disposition index from 8 to 11. Regression test for
        a critic-caught bug.
        """
        sample = (
            b'("message" "rfc822" NIL NIL NIL "7bit" 5000 '
            b'("Mon, 1 Jan 2024" "Subject" NIL NIL NIL NIL NIL NIL NIL NIL) '
            b'("text" "plain" NIL NIL NIL "7bit" 100 3 NIL NIL NIL NIL) 50 '
            b'NIL ("attachment" ("filename" "orig.eml")) NIL NIL)'
        )
        parts = parse_bodystructure(sample)
        assert len(parts) == 1
        assert parts[0]["type"] == "message/rfc822"
        assert parts[0]["size"] == 5000
        assert parts[0]["filename"] == "orig.eml"
        assert parts[0]["disposition"] == "attachment"

    def test_nested_multipart(self):
        """multipart/mixed containing multipart/alternative + attachment."""
        sample = (
            b"(("
            b'("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 100 3 NIL NIL NIL NIL)'
            b'("text" "html" ("charset" "utf-8") NIL NIL "7bit" 300 8 NIL NIL NIL NIL) '
            b'"alternative" ("boundary" "==alt") NIL NIL NIL)'
            b'("application" "pdf" ("name" "doc.pdf") NIL NIL "base64" 9999 NIL '
            b'("attachment" ("filename" "doc.pdf")) NIL NIL) '
            b'"mixed" ("boundary" "==mix") NIL NIL NIL)'
        )
        parts = parse_bodystructure(sample)
        # Nested: 1.1 = text/plain, 1.2 = text/html, 2 = pdf
        assert len(parts) == 3
        assert parts[0]["part"] == "1.1"
        assert parts[0]["type"] == "text/plain"
        assert parts[1]["part"] == "1.2"
        assert parts[1]["type"] == "text/html"
        assert parts[2]["part"] == "2"
        assert parts[2]["type"] == "application/pdf"

    def test_part_with_non_ascii_filename(self):
        """Filename with MIME encoded-word should be decoded."""
        # "=?utf-8?B?0L7RgtGH0ZHRgi5wZGY=?=" = "отчёт.pdf" base64 utf-8
        sample = (
            b'("application" "pdf" ("name" "=?utf-8?B?0L7RgtGH0ZHRgi5wZGY=?=") '
            b'NIL NIL "base64" 1000 NIL '
            b'("attachment" ("filename" "=?utf-8?B?0L7RgtGH0ZHRgi5wZGY=?=")) '
            b"NIL NIL)"
        )
        parts = parse_bodystructure(sample)
        assert len(parts) == 1
        assert parts[0]["filename"] == "отчёт.pdf"


class TestBodystructureHelpers:
    """Private parser internals."""

    def test_params_to_dict(self):
        assert _bodystructure_params_to_dict(
            ["charset", "utf-8", "name", "file.txt"]
        ) == {"charset": "utf-8", "name": "file.txt"}

    def test_params_to_dict_lowercase_keys(self):
        assert _bodystructure_params_to_dict(["CHARSET", "UTF-8"]) == {
            "charset": "UTF-8"
        }

    def test_params_to_dict_none(self):
        assert _bodystructure_params_to_dict(None) == {}

    def test_params_to_dict_empty(self):
        assert _bodystructure_params_to_dict([]) == {}


# =============================================================================
# Subject prefix dedupe
# =============================================================================


class TestDedupeRePrefix:
    def test_no_prefix(self):
        assert _dedupe_re_prefix("Hello") == "Re: Hello"

    def test_single_re(self):
        assert _dedupe_re_prefix("Re: Hello") == "Re: Hello"

    def test_lowercase_re(self):
        assert _dedupe_re_prefix("re: Hello") == "Re: Hello"

    def test_uppercase_re(self):
        assert _dedupe_re_prefix("RE: Hello") == "Re: Hello"

    def test_multiple_re(self):
        assert _dedupe_re_prefix("Re: Re: Re: Hello") == "Re: Hello"

    def test_mixed_case_multiple(self):
        assert _dedupe_re_prefix("RE: re: Re: Hello") == "Re: Hello"

    def test_with_spaces(self):
        assert _dedupe_re_prefix("Re : Hello") == "Re: Hello"

    def test_empty(self):
        assert _dedupe_re_prefix("") == "Re:"

    def test_none_safe(self):
        assert _dedupe_re_prefix(None) == "Re:"  # type: ignore[arg-type]


class TestDedupeFwdPrefix:
    def test_no_prefix(self):
        assert _dedupe_fwd_prefix("Hello") == "Fwd: Hello"

    def test_fwd(self):
        assert _dedupe_fwd_prefix("Fwd: Hello") == "Fwd: Hello"

    def test_fw(self):
        assert _dedupe_fwd_prefix("Fw: Hello") == "Fwd: Hello"

    def test_uppercase(self):
        assert _dedupe_fwd_prefix("FWD: Hello") == "Fwd: Hello"

    def test_multiple(self):
        assert _dedupe_fwd_prefix("FWD: Fw: Fwd: Hello") == "Fwd: Hello"

    def test_empty(self):
        assert _dedupe_fwd_prefix("") == "Fwd:"


# =============================================================================
# References trimming
# =============================================================================


class TestTrimReferences:
    def test_empty(self):
        assert _trim_references("") == ""

    def test_single(self):
        assert _trim_references("<a@x>") == "<a@x>"

    def test_short_chain(self):
        refs = "<a@x> <b@x> <c@x>"
        assert _trim_references(refs) == refs

    def test_long_chain_trimmed_to_max(self):
        refs = " ".join(f"<id{i}@test>" for i in range(20))
        result = _trim_references(refs)
        ids = result.split()
        # Default max_ids=10: root + last 9
        assert len(ids) == 10
        # Root preserved
        assert ids[0] == "<id0@test>"
        # Last 9 are id11–id19
        assert ids[-1] == "<id19@test>"

    def test_byte_limit_respected(self):
        """When max_bytes is tight, trim aggressively."""
        refs = " ".join(f"<very-long-message-id-{i}@example.com>" for i in range(20))
        result = _trim_references(refs, max_ids=10, max_bytes=200)
        assert len(result.encode("utf-8")) <= 200
        # Root still preserved
        assert result.startswith("<very-long-message-id-0@example.com>")


# =============================================================================
# Bulk helpers
# =============================================================================


class TestNormalizeUidList:
    def test_valid(self):
        assert _normalize_uid_list(["1", "2", "3"]) == ["1", "2", "3"]

    def test_whitespace_stripped(self):
        assert _normalize_uid_list(["  5  ", "10"]) == ["5", "10"]

    def test_int_coerced(self):
        assert _normalize_uid_list([1, 2, 3]) == ["1", "2", "3"]  # type: ignore[list-item]

    def test_large_uid(self):
        assert _normalize_uid_list(["4294967295"]) == ["4294967295"]

    def test_empty_list_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            _normalize_uid_list([])

    def test_zero_rejected(self):
        """UID 0 is invalid per RFC 3501 §2.3.1.1."""
        with pytest.raises(ValueError):
            _normalize_uid_list(["0"])

    def test_zero_in_middle_rejected(self):
        with pytest.raises(ValueError):
            _normalize_uid_list(["5", "0", "10"])

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            _normalize_uid_list(["-5"])

    def test_non_digit_rejected(self):
        with pytest.raises(ValueError):
            _normalize_uid_list(["abc"])

    def test_float_rejected(self):
        with pytest.raises(ValueError):
            _normalize_uid_list(["1.5"])

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            _normalize_uid_list([""])


class TestChunk:
    def test_exact_fit(self):
        assert list(_chunk(["a", "b", "c", "d"], 2)) == [["a", "b"], ["c", "d"]]

    def test_with_remainder(self):
        assert list(_chunk(["a", "b", "c", "d", "e"], 2)) == [
            ["a", "b"],
            ["c", "d"],
            ["e"],
        ]

    def test_chunk_larger_than_list(self):
        assert list(_chunk(["a", "b"], 10)) == [["a", "b"]]

    def test_empty(self):
        assert list(_chunk([], 5)) == []

    def test_single_element(self):
        assert list(_chunk(["only"], 3)) == [["only"]]


# =============================================================================
# Folder name helpers
# =============================================================================


class TestQuoteFolderForCommand:
    def test_plain_atom(self):
        assert _quote_folder_for_command("INBOX") == "INBOX"

    def test_with_space(self):
        assert _quote_folder_for_command("Sent Items") == '"Sent Items"'

    def test_utf7_encoded_passthrough(self):
        """Already UTF-7 encoded Cyrillic — all ASCII, no quoting needed."""
        assert _quote_folder_for_command("&BBIEMA-") == "&BBIEMA-"

    def test_with_embedded_quote(self):
        # "a\"b" — the embedded quote gets escaped
        assert _quote_folder_for_command('a"b') == '"a\\"b"'

    def test_with_backslash(self):
        assert _quote_folder_for_command("a\\b") == '"a\\\\b"'

    def test_empty(self):
        assert _quote_folder_for_command("") == '""'

    def test_with_paren(self):
        assert _quote_folder_for_command("(weird)") == '"(weird)"'


class TestEncodeFolderName:
    def test_ascii_pass_through(self):
        assert encode_folder_name("INBOX") == "INBOX"

    def test_ascii_with_spaces(self):
        assert encode_folder_name("Sent Items") == "Sent Items"

    def test_cyrillic_encoded(self):
        encoded = encode_folder_name("Корзина")
        # Result is ASCII-only UTF-7
        assert all(ord(c) < 128 for c in encoded)
        # Starts with & (IMAP modified UTF-7 marker)
        assert encoded.startswith("&")

    def test_roundtrip(self):
        original = "Отправленные"
        encoded = encode_folder_name(original)
        decoded = decode_folder_name(encoded)
        assert decoded == original

    def test_already_encoded_utf7_pass_through(self):
        """A name that's already UTF-7 encoded (pure ASCII) passes unchanged."""
        utf7 = "&BCcENQRABDcENwQwBDw-"  # Already encoded
        assert encode_folder_name(utf7) == utf7


class TestParseFolderLine:
    def test_quoted_name(self):
        line = b'(\\HasNoChildren) "/" "INBOX"'
        result = _parse_folder_line(line)
        assert result is not None
        attrs, name = result
        assert "\\HasNoChildren" in attrs
        assert name == "INBOX"

    def test_trash_attribute(self):
        line = b'(\\HasNoChildren \\Trash) "/" "Trash"'
        result = _parse_folder_line(line)
        assert result is not None
        attrs, name = result
        assert "\\Trash" in attrs
        assert name == "Trash"

    def test_sent_attribute(self):
        line = b'(\\Sent \\HasNoChildren) "/" "&BB4EMgQ7BDUEOwRDBDUEPQRCBDwBDsENwQwBDw-"'
        result = _parse_folder_line(line)
        assert result is not None
        attrs, name = result
        assert "\\Sent" in attrs

    def test_nil_delimiter(self):
        line = b"(\\HasNoChildren) NIL INBOX"
        result = _parse_folder_line(line)
        assert result is not None
        _attrs, name = result
        assert name == "INBOX"

    def test_trailing_whitespace(self):
        """Regression test: trailing CR shouldn't break the quoted-name branch."""
        line = b'(\\HasNoChildren) "/" "INBOX"\r'
        result = _parse_folder_line(line)
        assert result is not None
        _attrs, name = result
        assert name == "INBOX"

    def test_empty_attrs(self):
        line = b'() "/" "Folder"'
        result = _parse_folder_line(line)
        assert result is not None
        attrs, name = result
        assert attrs == []
        assert name == "Folder"

    def test_malformed_returns_none(self):
        assert _parse_folder_line(b"garbage") is None

    def test_none_input(self):
        assert _parse_folder_line(None) is None  # type: ignore[arg-type]


# =============================================================================
# Capability check
# =============================================================================


class _FakeConn:
    def __init__(self, caps):
        self.capabilities = caps


class TestHasCapability:
    def test_str_present(self):
        conn = _FakeConn(("IMAP4REV1", "MOVE", "IDLE"))
        assert _has_capability(conn, "MOVE") is True

    def test_str_absent(self):
        conn = _FakeConn(("IMAP4REV1", "IDLE"))
        assert _has_capability(conn, "MOVE") is False

    def test_case_insensitive(self):
        conn = _FakeConn(("IMAP4REV1", "move"))
        assert _has_capability(conn, "MOVE") is True

    def test_bytes_capability(self):
        conn = _FakeConn((b"IMAP4REV1", b"MOVE"))
        assert _has_capability(conn, "MOVE") is True

    def test_mixed_bytes_and_str(self):
        conn = _FakeConn((b"IMAP4REV1", "MOVE"))
        assert _has_capability(conn, "MOVE") is True

    def test_missing_attribute(self):
        class NoCaps:
            pass

        assert _has_capability(NoCaps(), "MOVE") is False

    def test_none_capabilities(self):
        conn = _FakeConn(None)
        assert _has_capability(conn, "MOVE") is False


# =============================================================================
# Flag validation (via _set_flags_impl — but we want to trigger validation
# BEFORE any network call, so we use invalid flags that raise ValueError
# before the imap_connection() call is reached)
# =============================================================================


class TestSetFlagsValidation:
    def test_empty_add_and_remove_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _set_flags_impl("INBOX", "1", add=None, remove=None)

    def test_empty_lists_raise(self):
        with pytest.raises(ValueError, match="non-empty"):
            _set_flags_impl("INBOX", "1", add=[], remove=[])

    def test_flag_with_space_rejected(self):
        with pytest.raises(ValueError, match="reserved"):
            _set_flags_impl("INBOX", "1", add=["bad flag"])

    def test_flag_with_paren_rejected(self):
        with pytest.raises(ValueError, match="reserved"):
            _set_flags_impl("INBOX", "1", add=["bad(flag"])

    def test_flag_with_quote_rejected(self):
        with pytest.raises(ValueError, match="reserved"):
            _set_flags_impl("INBOX", "1", add=['bad"flag'])

    def test_flag_empty_string_rejected(self):
        with pytest.raises(ValueError):
            _set_flags_impl("INBOX", "1", add=[""])

    def test_bare_backslash_rejected(self):
        with pytest.raises(ValueError):
            _set_flags_impl("INBOX", "1", add=["\\"])

    def test_remove_validation_also_runs(self):
        with pytest.raises(ValueError, match="reserved"):
            _set_flags_impl("INBOX", "1", remove=["bad flag"])

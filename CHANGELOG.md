# Changelog

All notable changes to this project will be documented in this file.

## [0.1.2] - 2026-04-11

PyPI publication readiness. The core functionality is unchanged; this release
focuses on making the package distributable via `pip install yandex-mail-mcp`
without collisions and adding publishing infrastructure.

### Added

- `LICENSE` file (MIT) — PyPI requires this, and it was missing from the
  original single-file project
- `.github/workflows/publish.yml` — GitHub Actions workflow that runs unit
  tests, builds sdist + wheel, validates metadata via `twine check`, and
  publishes to PyPI on `v*` tag push (or to TestPyPI via workflow_dispatch).
  Uses PyPI Trusted Publishing (OIDC) — no API token stored in secrets.
- CLAUDE.md now has a full `Publishing to PyPI` section with one-time setup,
  manual release workflow, and automated release via the CI workflow

### Changed

**BREAKING (for editable installs / source users):** the top-level module
has been renamed from `server` to `yandex_mail_mcp`. Users who had:

```python
from server import list_folders  # old
```

should update to:

```python
from yandex_mail_mcp import list_folders  # new
```

This is required for PyPI publication because `server.py` as a top-level
module would collide with any other package that ships a `server` module
in site-packages. The distribution name (`yandex-mail-mcp`) and the uvx
entry point (`yandex-mail-mcp`) are unchanged.

All test files have been updated to import from `yandex_mail_mcp`.
`pyproject.toml` entry point is now `yandex-mail-mcp = "yandex_mail_mcp:main"`.
`only-include` in the wheel target now points at `yandex_mail_mcp.py`.

### Verified

- `uv build` produces clean sdist + wheel
  (`yandex_mail_mcp-0.1.2.tar.gz` + `yandex_mail_mcp-0.1.2-py3-none-any.whl`)
- Wheel installed in a clean venv via `pip install dist/*.whl` exposes the
  `yandex-mail-mcp` console script and can be imported as `yandex_mail_mcp`
- `uvx --from . yandex-mail-mcp` builds and runs after the rename
- 124 unit tests pass against the renamed module
- `yandex-mail-mcp` PyPI name is currently available (checked 2026-04-11)

## [0.1.1] - 2026-04-11

Packaging for distribution via `uvx` and proper handling of credentials/logs when the code lives in site-packages rather than next to a user's `.env`.

### Added

- `pyproject.toml` with hatchling build backend, console script entry point `yandex-mail-mcp = "server:main"`, and proper metadata (keywords, classifiers, URLs)
- `main()` function in `server.py` so it can be used as both a console script and direct `python server.py` invocation
- Claude Desktop users can now launch via `uvx --from git+https://github.com/imdeniil/yandex-mail-mcp yandex-mail-mcp` with credentials in the MCP client's `env` config block — no clone, no venv, no manual `.env`

### Changed

- **`.env` loading** is no longer hardcoded to `SCRIPT_DIR/.env`. Resolution order is now:
  1. `$YANDEX_MAIL_MCP_ENV` override
  2. `$PWD/.env` (project-local)
  3. `$XDG_CONFIG_HOME/yandex-mail-mcp/.env`
  4. `SCRIPT_DIR/.env` (source checkouts only)

  Environment variables set in the MCP client config always take precedence because `load_dotenv()` does not override existing `os.environ`.

- **Log file location** is no longer hardcoded to `SCRIPT_DIR/yandex_mail_mcp.log`. Resolution order:
  1. `$YANDEX_MAIL_MCP_LOG_FILE` override
  2. `$XDG_STATE_HOME/yandex-mail-mcp/yandex_mail_mcp.log` (typically `~/.local/state/yandex-mail-mcp/`)
  3. `SCRIPT_DIR` if it's writable (source checkouts)
  4. `$TMPDIR` last-resort fallback

  This prevents crashes when `server.py` lives in a read-only site-packages directory.

- README restructured around uvx as the recommended install path, with traditional venv/editable install as the alternative for development

### Documentation

- CLAUDE.md: added packaging section, documented the new env/log resolution chains
- README: new "Quick Start with uvx" section with ready-to-paste Claude Desktop config, explained env var precedence, added "Not supported" section documenting empirically verified Yandex limitations (no ManageSieve, no SORT/THREAD)

## [0.1.0] - 2026-04-10

Major expansion from 7 to 28 MCP tools, full security audit, UID migration
for correctness, and comprehensive test coverage. Four separate code-review
cycles (critic agent) caught and fixed 2 BLOCKER + 3 MAJOR security issues,
3 BLOCKER + 2 MAJOR coverage-expansion bugs, and 2 BLOCKER + 2 MAJOR in the
BODYSTRUCTURE + bulk operations work.

### Added

**New inspection tools:**
- `get_folder_status(folder)` — IMAP STATUS with MESSAGES/UNSEEN/RECENT/
  UIDNEXT/UIDVALIDITY
- `get_unread_summary()` — unread + total counts across ALL selectable
  folders in a single IMAP session, with per-folder breakdown and summary
- `inspect_email(folder, email_id)` — fetches headers + BODYSTRUCTURE
  WITHOUT downloading bodies, returns subject/from/date/size + MIME part
  list with type/size/filename
- `fetch_part(folder, email_id, part_number)` — fetches a specific MIME
  part by number (e.g. "1", "2.1"), decodes base64/quoted-printable,
  returns text or base64

**New flag tools:**
- `set_flags(folder, email_id, add, remove)` — generic UID STORE with
  input validation (rejects flags with whitespace, parens, quotes)
- `mark_read` / `mark_unread` — \Seen flag
- `mark_flagged(folder, email_id, flagged)` — \Flagged star
- `mark_answered` — \Answered

**New folder management:**
- `create_folder(name)` — with UTF-7 encoding for Cyrillic names
- `rename_folder(old_name, new_name)`
- `delete_folder(name)` — behavior on non-empty folders is
  server-dependent per RFC 3501 §6.3.4

**New send tools:**
- `reply_email(folder, email_id, body, reply_all, html, attachments,
  save_to_sent)` — proper RFC 5322 threading with In-Reply-To and
  References headers, deduped Re: prefix, reply_all with RFC 5322
  address parsing and self-removal, best-effort \Answered flag on
  the original
- `forward_email(folder, email_id, to, body, as_attachment, ...)` —
  two modes: message/rfc822 attachment (default) or inline quoted
  body. Sanitized .eml filename (strips path separators, truncates
  to 80 chars). Dedupes Fwd:/Fw:/FWD: prefix.

**New bulk operations** (all chunk UIDs at ~500 per command to stay
under IMAP line-length limits):
- `bulk_set_flags(folder, email_ids, add, remove)` — with full flag
  validation
- `bulk_mark_read` / `bulk_mark_unread` / `bulk_mark_flagged`
- `bulk_move(folder, email_ids, destination)` — prefers atomic UID MOVE
  (RFC 6851) with graceful fallback to COPY+STORE+EXPUNGE mid-loop
- `bulk_delete(folder, email_ids, permanent)` — to Trash or permanent

**Convenience:**
- `empty_trash()` — discovers Trash via \Trash SPECIAL-USE, chunked
  UID STORE + EXPUNGE
- `send_email` now has `save_to_sent=True` parameter that appends to
  the Sent folder via IMAP APPEND after SMTP send (Yandex does not
  reliably auto-save sent messages)

**Extended search** in `search_emails` / `build_imap_search_criteria`:
- `LARGER <N>` / `SMALLER <N>` — size filtering
- `SENTSINCE` / `SENTBEFORE` / `SENTON` — filter by sent date
- `KEYWORD <flag>` / `UNKEYWORD <flag>` — filter by user keywords
- `HEADER <field> <value>` — dual-arg, multi-word values via shlex
- `OR` / `NOT` / parenthesized groups pass through
- `offset` parameter on `search_emails` for pagination
- `list_folders` now returns `attrs` field with IMAP folder attributes

### Changed

**BREAKING (semantically): All IMAP commands now use UIDs instead of
sequence numbers.** IDs returned by `search_emails` are stable within
a folder's UIDVALIDITY and will not shift after other messages are
deleted. Callers that persisted old sequence-number IDs must re-fetch.

- `move_email` and `delete_email` now prefer atomic UID MOVE (RFC 6851)
  when the server advertises the capability, with fallback to
  COPY+STORE+EXPUNGE
- `delete_email` discovers Trash via \Trash SPECIAL-USE (RFC 6154) with
  localized name fallbacks (Trash, Корзина, Deleted Items, Deleted
  Messages) instead of hardcoding "Trash"
- `send_email` has a new `attachments` parameter accepting a list of
  absolute file paths, with RFC 2231 encoding for non-ASCII filenames
- `imap_connection` refreshes capabilities after LOGIN to detect MOVE
  and other post-auth extensions
- `imap_connection` and `smtp_connection` now retry 3x with backoff on
  transient network errors (gaierror, timeout, ConnectionError, OSError)
- `list_folders` response includes `attrs` (e.g. `["\\Trash",
  "\\HasNoChildren"]`)
- `search_emails` query parser now uses shlex.split for robust quoted
  multi-word value handling
- Whitespace-only search query now correctly returns ["ALL"]

### Fixed

**Security (from initial review):**
- `download_attachment`: sanitized email-provided filenames to prevent
  path traversal (strip path components, verify resolved path stays
  within save_dir)
- `send_email`: documented attachment exfiltration risk prominently in
  README, added audit logging for every attached file path, resolved
  absolute paths
- Non-ASCII attachment filenames are now encoded per RFC 2231 via the
  keyword form of `Message.add_header()`

**Correctness:**
- Replaced fragile IMAP LIST response parser (`rsplit('"', 2)`) with
  robust `_parse_folder_line` handling quoted names, unquoted atoms,
  NIL delimiters, and trailing whitespace/CR
- `delete_email` same-folder comparison now normalizes both sides via
  `decode_folder_name() + casefold()` to avoid silent permanent-delete
  fallback when "trash" vs "Trash" or different UTF-7 encodings
- Move/delete no longer hardcode "Trash" — always search via SPECIAL-USE
- `read_email` guards against `None` payloads in text/plain, text/html,
  and non-multipart branches (was crashing on malformed MIME)
- UID SEARCH with Cyrillic charset uses a two-phase approach (legacy
  SEARCH + UID FETCH translate) because imaplib `_simple_command`
  with bytes args concatenates rather than sending literals
- BODYSTRUCTURE parser correctly handles `message/rfc822` parts, which
  have extra envelope/body/lines fields that push disposition to
  index 11 instead of the default 8 (regression fix per RFC 3501 §7.4.2)
- `inspect_email` now routes payloads by envelope marker (`BODY[HEADER`
  vs `BODYSTRUCTURE {N}`) instead of last-tuple-wins, avoiding clobber
  when BODYSTRUCTURE arrives as an IMAP literal
- `_extract_inline_bodystructure` walks paren depth with quote-state
  machine to return exactly the balanced s-expression, not trailing
  FETCH response tokens
- Folder name quoting for `UID MOVE` via `_simple_command` — names with
  spaces (e.g. "Sent Items") are now wrapped correctly
- `_set_flags_impl` validates flag strings to reject whitespace, parens,
  quotes, brackets — anything that would corrupt IMAP syntax
- `_normalize_uid_list` rejects UID 0 per RFC 3501 §2.3.1.1
- `reply_email` reply-all self-address removal uses proper RFC 5322
  parsing via `email.utils.getaddresses` (was naive substring match)
- `get_unread_summary` uses high-level `conn.status()` without
  pre-quoting folder names (avoids double-quote on names with spaces)

### Documentation

- CLAUDE.md updated with 28-tool inventory, test categories, run
  commands, Sieve/SORT/THREAD unavailability notes, rate-limit
  warnings
- README.md expanded with Security Notes section covering send_email
  attachment risk, download_attachment sanitization, .env credentials
  handling

### Tests

- `test_helpers.py` — 124 pure unit tests (offline), covering all
  critical helpers: search criteria parser, bodystructure tokenizer/
  parser/walker, prefix dedupe, references trimming, UID validation,
  chunking, folder name helpers, capability detection, flag validation
- `test_integration.py` — ~58 integration tests against live Yandex
  mailbox, organized by markers:
  - Unmarked (safe): get_folder_status, get_unread_summary,
    inspect_email, fetch_part, advanced search, download_attachment
  - `@pytest.mark.destructive`: folder management, flags, move, bulk
    operations, empty_trash
  - `@pytest.mark.send`: send, reply, forward
- `conftest.py` with `--run-destructive` CLI flag, marker registration,
  session fixtures including `sandbox_folder` with unique-per-run
  cleanup

## [0.0.1] - 2025-12-22

### Added
- Initial release of Yandex Mail MCP Server
- List folders with decoded Russian names (IMAP UTF-7)
- Search emails with IMAP queries (FROM, SUBJECT, UNSEEN, etc.)
- Cyrillic/UTF-8 search support
- Read email content (text/HTML body)
- Download attachments to disk
- Send emails (plain text or HTML)
- Move emails between folders
- Delete emails (move to Trash)
- Behavioral tests with pytest

### Documentation
- README with installation and usage instructions
- Claude Desktop setup guide (CLAUDE_DESKTOP.md)
- Example environment configuration (.env.example)

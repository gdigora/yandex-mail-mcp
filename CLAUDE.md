# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP (Model Context Protocol) server for Yandex Mail. Provides 28 email tools via IMAP/SMTP that can be used by any MCP-compatible client.

## Commands

```bash
# Setup (editable install for development)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Alternatively, run as a one-off via uvx without cloning:
uvx --from git+https://github.com/imdeniil/yandex-mail-mcp yandex-mail-mcp

# Safe tests only — unit tests + read-only integration (default)
pytest

# Verbose
pytest -v

# Unit tests only (no network at all)
pytest test_helpers.py

# Read-only integration only (requires .env credentials)
pytest test_integration.py

# DESTRUCTIVE + SEND tests — actually modifies the mailbox and sends mail.
# Opt in explicitly with --run-destructive. Tests use unique markers and
# clean up after themselves, but there's always some residual state risk.
pytest --run-destructive              # everything, including writes/sends
pytest -m destructive --run-destructive  # only destructive (folders, flags, moves)
pytest -m send --run-destructive         # only actual SMTP sends

# Single test class / case
pytest test_helpers.py::TestBodystructureParser -v
pytest test_integration.py::TestGetFolderStatus::test_has_counts -v

# Test server directly
python -c "from yandex_mail_mcp import list_folders; print(list_folders())"
```

## Architecture

Single-file MCP server (`yandex_mail_mcp.py`) using FastMCP framework. 28 `@mcp.tool()` functions + ~18 private helpers. The module is installed via the distribution name `yandex-mail-mcp` on PyPI.

**Tool categories:**
- **Read**: `list_folders`, `search_emails`, `read_email`, `download_attachment`, `get_folder_status`, `get_unread_summary`, `inspect_email`, `fetch_part`
- **Write**: `move_email`, `delete_email`, `set_flags` + `mark_read`/`mark_unread`/`mark_flagged`/`mark_answered`, `create_folder`/`rename_folder`/`delete_folder`, `empty_trash`
- **Bulk**: `bulk_set_flags`, `bulk_mark_read`/`mark_unread`/`mark_flagged`, `bulk_move`, `bulk_delete`
- **Send**: `send_email`, `reply_email`, `forward_email`

**Connection helpers**: `imap_connection()` / `smtp_connection()` context managers. IMAP refreshes `CAPABILITY` after login to detect MOVE extension.

**Encoding**: `encode_folder_name` / `decode_folder_name` for IMAP UTF-7 (Cyrillic folder names). `_quote_folder_for_command` for raw `_simple_command` paths.

**BODYSTRUCTURE parser**: `_tokenize_bodystructure` → `_parse_bodystructure_list` → `_walk_bodystructure` → `parse_bodystructure` (public). Part number assignment respects RFC 3501 §7.4.2 — including the message/rfc822 special case where disposition is at index 11 (not 8).

**Key implementation details:**
- `.env` resolved with fallback chain: `$YANDEX_MAIL_MCP_ENV` → `$PWD/.env` → `$XDG_CONFIG_HOME/yandex-mail-mcp/.env` → `SCRIPT_DIR/.env`. Env vars from the MCP client (Claude Desktop's `env` config block) always take precedence because `load_dotenv()` does not override existing `os.environ`.
- Logs go to `$YANDEX_MAIL_MCP_LOG_FILE` > `$XDG_STATE_HOME/yandex-mail-mcp/yandex_mail_mcp.log` > `SCRIPT_DIR` (if writable) > `$TMPDIR`. This makes the package uvx-installable: when running from site-packages, we don't try to write into read-only install dirs.
- stdout is reserved for MCP JSON-RPC protocol — never `print()`, always log to file
- All commands use IMAP UIDs (stable) rather than sequence numbers (unstable after EXPUNGE)
- Non-ASCII search takes a two-phase path: legacy `conn.search()` → seq numbers → UID FETCH translation, because `conn._simple_command` with bytes args concatenates rather than sending literals
- Atomic `UID MOVE` (RFC 6851) is preferred where capability is advertised, with graceful fallback to `COPY+STORE+EXPUNGE`

## Testing

### Three test files

| File | Category | Network? | Safe? |
|---|---|---|---|
| `test_server.py` | Legacy integration (21 tests) | Yes | Read-only, safe |
| `test_helpers.py` | Pure unit (~125 tests) | No | Always safe |
| `test_integration.py` | Full integration | Yes | Marker-gated |

### Test markers (in `test_integration.py`)

- **(unmarked)** — read-only. Runs by default. Never modifies mailbox state.
- **`@pytest.mark.destructive`** — creates/renames/deletes folders, changes flags, moves messages, empties trash. Requires `--run-destructive`.
- **`@pytest.mark.send`** — actually sends mail via SMTP (self-addressed with unique markers). Requires `--run-destructive`.

Test isolation: each destructive test uses a unique run_id + UUID and cleans up in `finally` blocks. The `sandbox_folder` fixture creates a unique MCP-Test-* folder per test and deletes it after.

### Integration test requirements

- `.env` with `YANDEX_EMAIL` + `YANDEX_APP_PASSWORD` — tests skip cleanly if missing
- Yandex rate-limits aggressively after many rapid connections. A full `pytest --run-destructive` run (21 tests, each opening 4-6 IMAP/SMTP sessions) will typically trip the limit partway through and subsequent tests fail with `ConnectionRefusedError: [Errno 111]` or `socket.gaierror`. The rate limit clears in ~10-30 minutes. Workarounds:
  - Run destructive tests in small subsets: `pytest test_integration.py::TestFolderManagement --run-destructive`
  - Space out runs by 30 minutes
  - `imap_connection()` / `smtp_connection()` now retry 3x on transient network errors, which covers brief DNS flakes but not sustained refusal

## Sieve and server-side filters

**NOTE:** Yandex does NOT expose ManageSieve (port 4190 closed, no SIEVE capability). Managing user mail filters ("правила обработки писем") via MCP is **impossible** — users must create them in the web UI at https://mail.yandex.ru/#setup/filters. Client-side automation (MCP + LLM deciding when to move/flag/delete) replaces server-side rules functionally but is not reactive.

## SORT/THREAD

Yandex does NOT implement SORT (RFC 5256) or THREAD. All three variants (`UID SORT`, `UID THREAD REFERENCES`, `UID THREAD ORDEREDSUBJECT`) return `BAD Command syntax error`. Client-side sorting was considered but dropped — if you need sorted results, sort the output of `search_emails` yourself.

## Packaging

Project is packaged via `pyproject.toml` using the hatchling build backend. Single-module layout (`yandex_mail_mcp.py` is the whole thing), with a console script entry point:

```toml
[project.scripts]
yandex-mail-mcp = "yandex_mail_mcp:main"
```

Local testing of the packaged build:
```bash
uvx --from . yandex-mail-mcp
```

Installing as an editable package in your venv:
```bash
pip install -e ".[dev]"    # includes pytest
```

Claude Desktop users launch it via uvx pointing at the git URL (no clone needed):
```json
{
  "command": "uvx",
  "args": ["--from", "git+https://github.com/imdeniil/yandex-mail-mcp", "yandex-mail-mcp"],
  "env": { "YANDEX_EMAIL": "...", "YANDEX_APP_PASSWORD": "..." }
}
```

## Publishing to PyPI

### One-time setup

1. **Create PyPI account** at https://pypi.org/account/register/
2. **Enable 2FA** on the account
3. **Create a project API token** after first upload (see manual upload below)
4. **Or: configure Trusted Publishing** at https://pypi.org/manage/account/publishing/ — more secure, no tokens stored anywhere. Register `imdeniil/yandex-mail-mcp` as a publisher with workflow file `.github/workflows/publish.yml` and environment `pypi`. The CI workflow in this repo is already set up for this.

### Manual release workflow

```bash
# 1. Bump version (edit both)
vim yandex_mail_mcp.py       # update VERSION = "..."
vim pyproject.toml           # update version = "..."

# 2. Update CHANGELOG.md with new section

# 3. Commit + tag
git add yandex_mail_mcp.py pyproject.toml CHANGELOG.md
git commit -m "chore: Release version X.Y.Z"
git tag -a vX.Y.Z -m "Release version X.Y.Z"

# 4. Push (triggers CI publish workflow if configured)
git push origin main
git push origin vX.Y.Z

# 5. Build artifacts locally (sanity check)
rm -rf dist/
uv build
# → produces dist/yandex_mail_mcp-X.Y.Z.tar.gz and .whl

# 6. Inspect artifacts
unzip -l dist/yandex_mail_mcp-*.whl
tar tzf dist/yandex_mail_mcp-*.tar.gz

# 7. (optional) Upload to TestPyPI first
uv publish --publish-url https://test.pypi.org/legacy/ --token <test-pypi-token>
# Verify in a clean venv:
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ yandex-mail-mcp
yandex-mail-mcp --version   # not a real flag, but confirms the script is wired up

# 8. Upload to real PyPI
uv publish --token <pypi-token>
# or: python -m twine upload dist/*

# 9. Verify install from PyPI
uvx yandex-mail-mcp
```

### Automated release (if GitHub Actions is configured)

Just push a `v*` tag — the workflow at `.github/workflows/publish.yml` triggers automatically, builds, runs smoke tests, and uploads via Trusted Publishing (no tokens needed in secrets).

```bash
# After editing version files and committing
git tag -a v0.1.2 -m "Release v0.1.2"
git push origin main v0.1.2
# → GitHub Actions picks up the tag, publishes to PyPI
```

### Version file synchronization

Version lives in two places that must be kept in sync:
- `yandex_mail_mcp.py`: `VERSION = "X.Y.Z"` — runtime constant, used for logging and debugging
- `pyproject.toml`: `version = "X.Y.Z"` — package metadata, what PyPI sees

(A future improvement could use `hatch-vcs` or `tool.hatch.version.source = "regex"` to derive pyproject version from the module constant.)

## Release

Use `/version X.Y.Z` command to release a new version. It updates `VERSION` in `yandex_mail_mcp.py`, creates CHANGELOG.md entry, commits, tags, and pushes.

**Note:** when bumping, also update `version` in `pyproject.toml` to match.

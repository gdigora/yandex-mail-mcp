# Yandex Mail MCP Server

MCP (Model Context Protocol) server for Yandex Mail. Enables Claude Desktop and other MCP clients to read, search, and manage emails via Yandex Mail — 28 tools covering every common mail workflow.

## Features

- **Folders** — list, create, rename, delete (with Cyrillic names via IMAP UTF-7)
- **Search** — full IMAP syntax: `FROM`/`TO`/`SUBJECT`/`BODY`, `LARGER`/`SMALLER`, `SENTSINCE`/`SENTBEFORE`, `HEADER <field> <value>`, `KEYWORD`/`UNKEYWORD`, `OR`/`NOT`. Cyrillic queries supported.
- **Read** — full content, text + HTML body, attachment list
- **Inspect** — fetch MIME structure + size WITHOUT downloading bodies (`inspect_email`/`fetch_part`) — critical for large messages
- **Flags** — `mark_read`/`mark_unread`/`mark_flagged`/`mark_answered` + generic `set_flags`
- **Send** — plain/HTML, attachments (RFC 2231 for non-ASCII names), save-to-Sent
- **Reply** — proper `In-Reply-To`/`References` threading, deduped `Re:` prefix, `reply_all` with RFC 5322 address parsing
- **Forward** — as `message/rfc822` attachment or inline quoted body
- **Move/Delete** — atomic `UID MOVE` (RFC 6851) when supported, smart Trash discovery via `\Trash` SPECIAL-USE
- **Bulk** — `bulk_move`/`bulk_delete`/`bulk_set_flags` etc. — chunked UID operations for batch workflows
- **Convenience** — `empty_trash`, `get_unread_summary` (counts across all folders in one session)

All operations use stable IMAP UIDs (not sequence numbers), and connection helpers retry transiently on DNS/network flakes.

## Quick Start with uvx (recommended)

No install, no venv — `uvx` fetches the package and runs it sandboxed. Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%/Claude/claude_desktop_config.json` (Windows):

### Option 1: From PyPI

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "uvx",
      "args": ["yandex-mail-mcp"],
      "env": {
        "YANDEX_EMAIL": "your-address@yandex.ru",
        "YANDEX_APP_PASSWORD": "your-app-password-here"
      }
    }
  }
}
```

### Option 2: From GitHub (latest development build)

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/imdeniil/yandex-mail-mcp",
        "yandex-mail-mcp"
      ],
      "env": {
        "YANDEX_EMAIL": "your-address@yandex.ru",
        "YANDEX_APP_PASSWORD": "your-app-password-here"
      }
    }
  }
}
```

Restart Claude Desktop. The server will appear as `yandex-mail` with 28 tools available.

To pin to a specific release:

```json
"--from", "git+https://github.com/imdeniil/yandex-mail-mcp@v0.1.1"
```

### Getting a Yandex app password

1. Go to [Yandex ID](https://id.yandex.ru/)
2. Enable **Two-Factor Authentication** (required for app passwords)
3. Go to **Security → App Passwords**
4. Create new app password for "Mail"
5. Paste the generated password into `YANDEX_APP_PASSWORD` above

## Alternative: Install from source

If you want to hack on the code or don't want uvx:

```bash
git clone https://github.com/imdeniil/yandex-mail-mcp.git
cd yandex-mail-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # installs as editable package with deps
# or for dev tools too:
pip install -e ".[dev]"

cp .env.example .env
# Edit .env with your Yandex email and app password
```

Then point Claude Desktop at the venv's Python:

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "/absolute/path/to/yandex-mail-mcp/.venv/bin/yandex-mail-mcp"
    }
  }
}
```

## Configuration

### Credentials

The server looks for credentials in this order (first wins):

1. **Environment variables** `YANDEX_EMAIL` / `YANDEX_APP_PASSWORD` (best for uvx + Claude Desktop)
2. `$YANDEX_MAIL_MCP_ENV` override path to a `.env` file
3. `$PWD/.env` (project-local, for direct invocation)
4. `$XDG_CONFIG_HOME/yandex-mail-mcp/.env` (typically `~/.config/yandex-mail-mcp/.env`)
5. `.env` next to `yandex_mail_mcp.py` (source checkout)

For Claude Desktop + uvx, just put them in the `env` block of the config as shown above.

### Log file location

The server writes to a log file (stdout is reserved for MCP protocol). Resolution order:

1. `$YANDEX_MAIL_MCP_LOG_FILE` override
2. `$XDG_STATE_HOME/yandex-mail-mcp/yandex_mail_mcp.log` (typically `~/.local/state/yandex-mail-mcp/yandex_mail_mcp.log`)
3. Next to `yandex_mail_mcp.py` in source checkouts
4. `$TMPDIR/yandex_mail_mcp.log` last-resort fallback

## Available Tools

28 tools across 6 categories. See `CHANGELOG.md` for the full list. Key ones:

| Tool | Purpose |
|---|---|
| `list_folders()` | Enumerate mailbox folders with attrs |
| `get_unread_summary()` | Unread counts across all folders |
| `search_emails(folder, query, limit, offset)` | IMAP query with pagination |
| `inspect_email(folder, email_id)` | Headers + MIME structure, no body download |
| `fetch_part(folder, email_id, part_number)` | Download a specific MIME part |
| `read_email(folder, email_id)` | Full text + HTML + attachments |
| `send_email(to, subject, body, cc, bcc, html, attachments)` | Send |
| `reply_email(folder, email_id, body, reply_all, ...)` | Reply with threading |
| `forward_email(folder, email_id, to, body, as_attachment, ...)` | Forward |
| `move_email` / `delete_email` | Atomic where possible |
| `mark_read` / `mark_unread` / `mark_flagged` / `mark_answered` | Flag shortcuts |
| `bulk_move` / `bulk_delete` / `bulk_set_flags` | Batch operations |
| `create_folder` / `rename_folder` / `delete_folder` | Mailbox management |
| `empty_trash()` | One-call trash cleanup |

## Search Query Examples

```
ALL                                  # All emails
UNSEEN                               # Unread
FROM sender@example.com              # From specific sender
SUBJECT hello                        # Subject contains "hello"
SINCE 01-Dec-2024                    # Received since date
SENTSINCE 01-Jan-2024                # Sent since date
LARGER 1048576                       # Larger than 1 MB
HEADER List-Id announce              # Custom header search
HEADER X-Custom "multi word value"   # Multi-word via shlex
KEYWORD Important                    # User keyword flag
UNSEEN FROM boss@company.com         # Combined (implicit AND)
OR FROM alice@x.com FROM bob@x.com   # Logical OR
NOT DELETED                          # Negation
UNSEEN LARGER 500000 SINCE 01-Jan-2024  # Multi-criteria
```

## Running Tests

```bash
# Install dev deps
pip install -e ".[dev]"

# Safe tests (always run — unit + read-only integration)
pytest

# Full suite including destructive + send (modifies mailbox, sends mail)
pytest --run-destructive

# Specific category
pytest -m destructive --run-destructive
pytest -m send --run-destructive
```

Integration tests require `.env` with valid credentials. Destructive and send tests are gated behind `--run-destructive` for safety.

## Security Notes

- **`send_email` attachments can read any file accessible to the server process.** The `attachments` parameter accepts absolute file paths, so in principle an LLM could be prompt-injected (e.g. via the body of an incoming email read through `read_email`) into attaching sensitive files such as `~/.ssh/id_rsa` to an outgoing message. This is inherent to exposing a filesystem-reading primitive over MCP.

  **Mitigations:**
  - Every `send_email` call must be approved by you in the MCP client (Claude Desktop shows tool calls before executing them — **always read which files are being attached before approving**).
  - Every attachment path is written to the log file for audit.
  - Run the server as a user that only has access to files you are willing to send by email.

- **`download_attachment` sanitises filenames** from received email (strips path components, asserts the resolved path stays within `save_dir`) so a malicious sender cannot write outside the target directory.

- **`delete_folder` is destructive.** Behavior on non-empty folders is server-dependent per RFC 3501 §6.3.4. Approve carefully.

- **Credentials** come from environment variables (MCP client config) or a `.env` file. Keep `.env` out of version control.

## Not supported (intentionally)

Verified empirically against `imap.yandex.com`:

- **ManageSieve / server-side filters** — Yandex does not expose the ManageSieve protocol (port 4190 closed, no `SIEVE` capability). User filter rules ("Правила обработки писем") can only be managed through the Yandex web UI. This MCP server provides client-side equivalents via `bulk_*` + conditional logic.
- **SORT / THREAD extensions (RFC 5256)** — Yandex returns `BAD Command syntax error`. Sort client-side if needed.
- **IDLE push notifications** — supported by Yandex but not exposed as an MCP tool because long-polling doesn't fit the stateless request/response model. Use `get_folder_status` or `get_unread_summary` for polling instead.

## License

MIT

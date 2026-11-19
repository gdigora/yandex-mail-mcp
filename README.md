# Yandex Mail MCP Server

MCP (Model Context Protocol) server for Yandex Mail. Enables Claude Desktop and other MCP clients to read, search, and send emails via Yandex Mail.

## Features

- **List folders** — with decoded Russian folder names
- **Search emails** — by sender, subject, date, or custom IMAP queries (supports Cyrillic)
- **Read emails** — full content with text/HTML body
- **Download attachments** — save to disk
- **Send emails** — plain text or HTML
- **Move/Delete emails** — organize your mailbox

## Installation

```bash
# Clone the repository
git clone https://github.com/gdigora/yandex-mail-mcp.git
cd yandex-mail-mcp

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your Yandex email and app password
```

## Security Model

This server gives the connected MCP client (your LLM host, e.g. Claude Desktop) **full
authority over one mailbox** — read, search, move, delete, and send as you — and lets it
write downloaded attachments to local directories. Keep in mind:

- **Enable tool approval in your MCP client** and do not set "Always allow" for
  `send_email`, `delete_email`, `move_email`, or `download_attachment`: emails from
  unauthenticated senders can contain instructions aimed at the model
  (indirect prompt injection). Every tool result from this server is labeled
  `provenance: "untrusted_remote"` for that reason.
- Credentials live only in the local `.env` file (gitignored) and are transmitted over
  TLS with full certificate and hostname verification.
- Attachment downloads are confined to the target directory (traversal and symlink
  escapes are rejected), and destructive operations are fail-safe: a failed move to
  Trash raises instead of permanently deleting, and expunging is scoped so other
  pending-deleted messages in the folder are never swept.

## Yandex Setup

1. Go to [Yandex ID](https://id.yandex.ru/)
2. Enable **Two-Factor Authentication** (required for app passwords)
3. Go to **Security → App Passwords**
4. Create new app password for "Mail"
5. Copy the generated password to `.env`

## Claude Desktop Configuration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "/path/to/yandex-mail-mcp/.venv/bin/python",
      "args": ["/path/to/yandex-mail-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop after configuration.

## Available Tools

| Tool | Description |
|------|-------------|
| `list_folders()` | List all mailbox folders |
| `search_emails(folder, query, limit)` | Search emails with IMAP queries |
| `read_email(folder, email_id)` | Read full email content |
| `download_attachment(folder, email_id, filename, save_dir)` | Download attachment to disk |
| `send_email(to, subject, body, cc, bcc, html)` | Send an email |
| `move_email(folder, email_id, destination)` | Move email to another folder |
| `delete_email(folder, email_id)` | Delete email (move to Trash) |

## Search Query Examples

```
ALL                          # All emails
UNSEEN                       # Unread emails
FROM sender@example.com      # From specific sender
SUBJECT hello                # Subject contains "hello"
SINCE 01-Dec-2024            # Emails since date
UNSEEN FROM boss@company.com # Combined query
```

## Running Tests

Offline regression tests (no network, no credentials needed — stubbed IMAP):

```bash
source .venv/bin/activate
pytest test_security_fixes.py test_correctness_fixes.py -v
```

`test_server.py` contains integration tests against a live Yandex mailbox and requires
valid credentials in `.env` (may fail due to Yandex rate limiting).

## License

MIT — see [LICENSE](LICENSE).

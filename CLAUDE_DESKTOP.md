# Claude Desktop Setup

Quick guide to configure Yandex Mail MCP for Claude Desktop.

## Prerequisites

- [Yandex app password](https://id.yandex.ru/security/app-passwords) for Mail
- (Optional) [uv](https://docs.astral.sh/uv/) installed — provides `uvx` for zero-install launch

## Option 1: uvx from PyPI (recommended — zero install)

Once the package is published to PyPI:

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "uvx",
      "args": ["yandex-mail-mcp"],
      "env": {
        "YANDEX_EMAIL": "your-address@yandex.ru",
        "YANDEX_APP_PASSWORD": "your-app-password"
      }
    }
  }
}
```

Paths by platform:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

## Option 2: uvx from GitHub (latest dev)

Same config, different `args`:

```json
"args": ["--from", "git+https://github.com/imdeniil/yandex-mail-mcp", "yandex-mail-mcp"]
```

To pin to a specific release: `"git+https://github.com/imdeniil/yandex-mail-mcp@v0.1.2"`

## Option 3: Editable install (for development)

```bash
git clone https://github.com/imdeniil/yandex-mail-mcp.git
cd yandex-mail-mcp
python3 -m venv .venv
source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

Then point Claude Desktop at the venv's installed console script:

### macOS / Linux

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "/absolute/path/to/yandex-mail-mcp/.venv/bin/yandex-mail-mcp",
      "env": {
        "YANDEX_EMAIL": "your-address@yandex.ru",
        "YANDEX_APP_PASSWORD": "your-app-password"
      }
    }
  }
}
```

### Windows

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "C:\\path\\to\\yandex-mail-mcp\\.venv\\Scripts\\yandex-mail-mcp.exe",
      "env": {
        "YANDEX_EMAIL": "your-address@yandex.ru",
        "YANDEX_APP_PASSWORD": "your-app-password"
      }
    }
  }
}
```

Credentials can also come from a `.env` file in the project directory, at `~/.config/yandex-mail-mcp/.env`, or at a path given by `$YANDEX_MAIL_MCP_ENV`. Env vars from the `env` config block above always take precedence.

## Verify Installation

1. Restart Claude Desktop (Cmd+Q / Alt+F4, then reopen)
2. Look for "yandex-mail" in the MCP servers list
3. Try asking Claude: *"List my email folders"* or *"Show me unread count across all folders"*

## Troubleshooting

### Server disconnected

Check logs at:
- **macOS:** `~/Library/Logs/Claude/mcp*.log`
- **Windows:** `%APPDATA%\Claude\logs\mcp*.log`

And the server's own log file:
- **macOS/Linux:** `~/.local/state/yandex-mail-mcp/yandex_mail_mcp.log`
- **Windows:** `%LOCALAPPDATA%\yandex-mail-mcp\yandex_mail_mcp.log`

Common issues:
- Missing or wrong credentials in the `env` config block
- `uvx` not on PATH — install uv from https://docs.astral.sh/uv/
- For Option 3: absolute path to `.venv` console script is wrong

### Tools not appearing

- Ensure Claude Desktop is fully restarted
- Check that the config JSON is valid (no trailing commas, balanced braces)
- Check the MCP log in Claude Desktop for stderr from the server process

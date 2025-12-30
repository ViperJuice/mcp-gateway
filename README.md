# PMCP - Progressive MCP

<!-- mcp-name: io.github.ViperJuice/pmcp -->

[![PyPI version](https://badge.fury.io/py/pmcp.svg)](https://pypi.org/project/pmcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Progressive disclosure for MCP** - Minimal context bloat with on-demand tool discovery and dynamic server provisioning.

## The Problem

When Claude Code connects directly to multiple MCP servers (GitHub, Jira, DB, etc.), it loads **all** tool schemas into context. This causes:
- **Context bloat**: Dozens of tool definitions consume tokens before you even ask a question
- **Static configuration**: Requires Claude Code restart to see new servers
- **No progressive disclosure**: Full schemas shown even when not needed

Anthropic has [highlighted context bloat](https://www.anthropic.com/news) as a key challenge with MCP tooling.

## The Solution

**PMCP** acts as a single MCP server that Claude Code connects to. Instead of exposing all downstream tools, it provides:

- **9 stable meta-tools** (not the 50+ underlying tools)
- **Auto-starts** essential servers (Playwright, Context7) with no configuration
- **Dynamically provisions** new servers on-demand from a manifest of 25+
- **Progressive disclosure**: Compact capability cards first, detailed schemas only on request
- **Policy enforcement**: Output size caps and optional secret redaction

## Quick Start

### Installation

```bash
# With pip
pip install pmcp

# With uv (recommended)
uv pip install pmcp

# Or run directly with uvx
uvx pmcp

# With LLM capability matching (optional)
pip install pmcp[llm]
```

### Configure Claude Code

Create/update `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "gateway": {
      "command": "pmcp",
      "args": []
    }
  }
}
```

That's it! PMCP auto-starts with Playwright and Context7 servers ready to use.

### Your First Interaction

```
You: "Take a screenshot of google.com"

Claude uses: gateway.invoke {
  tool_id: "playwright::browser_navigate",
  arguments: { url: "https://google.com" }
}
// Then: gateway.invoke { tool_id: "playwright::browser_screenshot" }

Returns: Screenshot of google.com
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Claude Code                          │
│  Only connects to PMCP (single server in config)            │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                          PMCP                               │
│  • 9 meta-tools (catalog, invoke, provision, etc.)          │
│  • Progressive disclosure (compact cards → full schemas)    │
│  • Policy enforcement (allow/deny lists)                    │
└────────────────────────────┬────────────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
┌───────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Auto-Start   │  │    Manifest     │  │  Custom Servers │
│  (Playwright, │  │   (25+ servers  │  │  (your own MCP  │
│   Context7)   │  │   on-demand)    │  │  servers)       │
└───────────────┘  └─────────────────┘  └─────────────────┘
```

**Key principle**: Users configure ONLY `pmcp` in Claude Code.
The gateway discovers and manages all other servers.

### Why Single-Gateway?

1. **No context bloat** - Claude sees 9 tools, not 50+
2. **No restarts** - Provision new servers without restarting Claude Code
3. **Consistent interface** - All tools accessed via `gateway.invoke`
4. **Policy control** - Centralized allow/deny rules

## Gateway Tools

The gateway exposes **9 meta-tools** organized into two categories:

### Core Tools

| Tool | Purpose |
|------|---------|
| `gateway.catalog_search` | Search available tools, returns compact capability cards |
| `gateway.describe` | Get detailed schema for a specific tool |
| `gateway.invoke` | Call a downstream tool with argument validation |
| `gateway.refresh` | Reload backend configs and reconnect |
| `gateway.health` | Get gateway and server health status |

### Capability Discovery Tools

| Tool | Purpose |
|------|---------|
| `gateway.request_capability` | Natural language capability matching with CLI preference |
| `gateway.sync_environment` | Detect platform and available CLIs |
| `gateway.provision` | Install and start MCP servers on-demand |
| `gateway.provision_status` | Check installation progress |

## Progressive Disclosure Workflow

PMCP follows a progressive disclosure pattern - start with natural language, get recommendations, drill down as needed.

### Step 1: Request a Capability

```
You: "I need to look up library documentation"

gateway.request_capability({ query: "library documentation" })
```

Returns:
```json
{
  "status": "candidates",
  "candidates": [{
    "name": "context7",
    "candidate_type": "server",
    "relevance_score": 0.95,
    "is_running": true,
    "reasoning": "Context7 provides up-to-date documentation for any package"
  }],
  "recommendation": "Use context7 - already running"
}
```

### Step 2: Search Available Tools

```
gateway.catalog_search({ query: "documentation" })
```

### Step 3: Get Tool Details

```
gateway.describe({ tool_id: "context7::get-library-docs" })
```

### Step 4: Invoke the Tool

```
gateway.invoke({
  tool_id: "context7::get-library-docs",
  arguments: { libraryId: "/npm/react/19.0.0" }
})
```

## Dynamic Server Provisioning

PMCP can install and start MCP servers on-demand from a curated manifest of 25+ servers.

### Example: Adding GitHub Support

```
You: "I need to manage GitHub issues"

gateway.request_capability({ query: "github issues" })
```

Returns (if not already configured):
```json
{
  "status": "candidates",
  "candidates": [{
    "name": "github",
    "candidate_type": "server",
    "is_running": false,
    "requires_api_key": true,
    "env_var": "GITHUB_PERSONAL_ACCESS_TOKEN",
    "env_instructions": "Create at https://github.com/settings/tokens with repo scope"
  }]
}
```

### Provisioning

```bash
# 1. Set API key (if required)
export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...

# 2. Provision via gateway
gateway.provision({ server_name: "github" })
```

## Auto-Start Servers

These servers start automatically (no configuration required):

| Server | Description | API Key |
|--------|-------------|---------|
| `playwright` | Browser automation - navigation, screenshots, DOM inspection | Not required |
| `context7` | Library documentation lookup - up-to-date docs for any package | Optional (for higher rate limits) |

## Available Servers

The manifest includes 25+ servers that can be provisioned on-demand:

### No API Key Required

| Server | Description |
|--------|-------------|
| `filesystem` | File operations - read, write, search |
| `memory` | Persistent knowledge graph |
| `fetch` | HTTP requests with robots.txt compliance |
| `sequential-thinking` | Problem solving through thought sequences |
| `git` | Git operations via MCP |
| `sqlite` | SQLite database operations |
| `time` | Timezone operations |
| `puppeteer` | Headless Chrome automation |

### Requires API Key

| Server | Description | Environment Variable |
|--------|-------------|---------------------|
| `github` | GitHub API - issues, PRs, repos | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| `gitlab` | GitLab API - projects, MRs | `GITLAB_PERSONAL_ACCESS_TOKEN` |
| `slack` | Slack messaging | `SLACK_BOT_TOKEN` |
| `notion` | Notion workspace | `NOTION_TOKEN` |
| `linear` | Linear issue tracking | `LINEAR_API_KEY` |
| `postgres` | PostgreSQL database | `POSTGRES_URL` |
| `brave-search` | Web search | `BRAVE_API_KEY` |
| `google-drive` | Google Drive files | `GDRIVE_CREDENTIALS` |
| `sentry` | Error tracking | `SENTRY_AUTH_TOKEN` |

See `.env.example` for all supported environment variables.

## Configuration

### Config Discovery

PMCP discovers MCP servers from:

1. **Project config**: `.mcp.json` in project root (highest priority)
2. **User config**: `~/.mcp.json` or `~/.claude/.mcp.json`
3. **Custom config**: Via `--config` flag or `PMCP_CONFIG` env var

### Adding Custom Servers

For MCP servers not in the manifest, add them to `~/.mcp.json`:

```json
{
  "mcpServers": {
    "my-custom-server": {
      "command": "node",
      "args": ["./my-server.js"],
      "env": {
        "API_KEY": "..."
      }
    }
  }
}
```

**Important**: Don't add `pmcp` itself to this file. PMCP is configured
in Claude Code's config (`~/.claude/mcp.json`), not in the downstream server list.

### Policy File

Create a policy file to control access and limits:

**~/.claude/gateway-policy.yaml**:
```yaml
servers:
  allowlist: []  # Empty = allow all
  denylist:
    - dangerous-server

tools:
  denylist:
    - "*::delete_*"
    - "*::drop_*"

limits:
  max_tools_per_server: 100
  max_output_bytes: 50000
  max_output_tokens: 4000

redaction:
  patterns:
    - "(api[_-]?key)[\\s]*[:=][\\s]*[\"']?([^\\s\"']+)"
    - "(password|secret)[\\s]*[:=][\\s]*[\"']?([^\\s\"']+)"
```

### CLI Commands

```bash
# Start the gateway server (default)
pmcp

# Check server status
pmcp status
pmcp status --json              # JSON output
pmcp status --server playwright # Filter by server

# View logs
pmcp logs
pmcp logs --follow              # Live tail
pmcp logs --tail 100            # Last 100 lines

# Refresh server connections
pmcp refresh
pmcp refresh --server github    # Refresh specific server
pmcp refresh --force            # Force reconnect all

# Initialize config (interactive)
pmcp init
```

## Docker

```bash
# Using Docker
docker run -it --rm \
  -v ~/.mcp.json:/home/appuser/.mcp.json:ro \
  -v ~/.env:/app/.env:ro \
  ghcr.io/viperjuice/pmcp:latest

# Using Docker Compose
docker-compose up -d
```

## Development

```bash
# Clone the repo
git clone https://github.com/ViperJuice/pmcp
cd pmcp

# Install with uv (recommended)
uv sync --all-extras

# Run tests
uv run pytest

# Run with debug logging
uv run pmcp --debug
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=pmcp

# Run specific test file
uv run pytest tests/test_policy.py -v
```

### Project Structure

```
pmcp/
├── src/pmcp/
│   ├── __init__.py
│   ├── __main__.py           # python -m pmcp entry
│   ├── cli.py                # CLI commands (status, logs, init, refresh)
│   ├── server.py             # MCP server implementation
│   ├── config/
│   │   └── loader.py         # Config discovery (.mcp.json)
│   ├── client/
│   │   └── manager.py        # Downstream server connections
│   ├── policy/
│   │   └── policy.py         # Allow/deny lists
│   ├── tools/
│   │   └── handlers.py       # Gateway tool implementations
│   ├── manifest/
│   │   ├── manifest.yaml     # Server manifest (25+ servers)
│   │   ├── loader.py         # Manifest loading
│   │   ├── installer.py      # Server provisioning
│   │   └── environment.py    # Platform/CLI detection
│   └── baml_client/          # BAML-generated LLM client (optional)
├── tests/                    # 310+ tests
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── README.md
```

## Troubleshooting

### Server Won't Connect

```bash
pmcp status
pmcp logs --level debug
pmcp refresh --force
```

### Missing API Key

```bash
# Check which key is needed
pmcp status --server github

# Set the key
export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...
```

### Tool Invocation Fails

```
gateway.catalog_search({ query: "tool-name" })
gateway.describe({ tool_id: "server::tool-name" })
gateway.list_pending()
```

## License

MIT

# MCP Gateway

<!-- mcp-name: io.github.ViperJuice/gateway-mcp -->

[![PyPI version](https://badge.fury.io/py/gateway-mcp.svg)](https://pypi.org/project/gateway-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A meta-server for minimal Claude Code tool bloat with progressive disclosure and dynamic server provisioning.

## Why This Exists

When Claude Code connects directly to multiple MCP servers (GitHub, Jira, DB, etc.), it loads **all** tool schemas into context. This causes:
- **Tool bloat**: Dozens of tool definitions consume context tokens
- **Static configuration**: Requires Claude Code restart to see new servers
- **No progressive disclosure**: Full schemas shown even when not needed

**MCP Gateway solves this** by acting as a single MCP server that Claude Code connects to. The gateway:
- Exposes only **9 stable meta-tools** (not the underlying tools)
- **Auto-starts** essential servers (Playwright, Context7) with no configuration
- **Dynamically provisions** new servers on-demand from a manifest of 25+
- Returns **compact capability cards** first, detailed schemas only on request
- Enforces output size caps and optional secret redaction

## Quick Start

### Installation

```bash
# With pip
pip install gateway-mcp

# With uv (recommended)
uv pip install gateway-mcp

# Or run directly with uvx
uvx gateway-mcp

# With LLM capability matching (optional)
pip install gateway-mcp[llm]
uv pip install gateway-mcp[llm]
```

### Configure Claude Code

Create/update `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "gateway": {
      "command": "mcp-gateway",
      "args": []
    }
  }
}
```

That's it! The gateway auto-starts with Playwright and Context7 servers ready to use.

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

## Auto-Start Servers

These servers start automatically with the gateway (no configuration required):

| Server | Description | API Key |
|--------|-------------|---------|
| `playwright` | Browser automation - navigation, screenshots, DOM inspection | Not required |
| `context7` | Library documentation lookup - up-to-date docs for any package | Optional (for higher rate limits) |

To disable auto-start servers, add them to your policy denylist:

```yaml
# ~/.claude/gateway-policy.yaml
servers:
  denylist:
    - playwright
    - context7
```

## Progressive Disclosure Workflow

MCP Gateway follows a progressive disclosure pattern - start with natural language, get recommendations, drill down as needed.

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

MCP Gateway can install and start MCP servers on-demand from a curated manifest of 25+ servers.

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

Returns:
```json
{
  "ok": true,
  "status": "started",
  "job_id": "abc123",
  "message": "Installation started. Poll gateway.provision_status for progress."
}
```

### Check Progress

```
gateway.provision_status({ job_id: "abc123" })
```

## Available Servers

The gateway includes a manifest of 25+ servers that can be provisioned on-demand:

### No API Key Required

| Server | Description |
|--------|-------------|
| `playwright` | Browser automation (auto-start) |
| `context7` | Library documentation (auto-start) |
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

The gateway discovers MCP servers from:

1. **Project config**: `.mcp.json` in project root (highest priority)
2. **User config**: `~/.mcp.json` or `~/.claude/.mcp.json`
3. **Custom config**: Via `--config` flag or `MCP_GATEWAY_CONFIG` env var

Project configs override user configs when server names collide.

### Policy File

Create a policy file to control access and limits:

**~/.claude/gateway-policy.yaml**:
```yaml
servers:
  # Only allow specific servers (empty = allow all)
  allowlist: []
  # Block specific servers
  denylist:
    - dangerous-server

tools:
  # Block dangerous tool patterns
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
mcp-gateway

# Check server status
mcp-gateway status
mcp-gateway status --json              # JSON output
mcp-gateway status --server playwright # Filter by server
mcp-gateway status --pending           # Show pending requests

# View logs
mcp-gateway logs
mcp-gateway logs --follow              # Live tail
mcp-gateway logs --tail 100            # Last 100 lines
mcp-gateway logs --level error         # Filter by level

# Refresh server connections
mcp-gateway refresh
mcp-gateway refresh --server github    # Refresh specific server
mcp-gateway refresh --force            # Force reconnect all

# Initialize config (interactive)
mcp-gateway init
mcp-gateway init --project ./myapp     # Specify project directory
mcp-gateway init --force               # Overwrite existing config
```

### CLI Options

```
mcp-gateway [OPTIONS] [COMMAND]

OPTIONS:
  -h, --help              Show help
  -p, --project <path>    Project root for .mcp.json discovery
  -c, --config <path>     Custom MCP config file
  --policy <path>         Policy file (YAML or JSON)
  -l, --log-level <level> debug|info|warn|error (default: info)
  --debug                 Enable debug logging
  -q, --quiet             Only show errors

COMMANDS:
  status                  Show server and tool status
  logs                    View gateway logs
  refresh                 Reload configurations
  init                    Initialize project config

ENVIRONMENT:
  MCP_GATEWAY_CONFIG      Custom config file path
  MCP_GATEWAY_POLICY      Policy file path
  MCP_GATEWAY_LOG_LEVEL   Log level
```

## Docker

Run the gateway in a container:

```bash
# Using Docker
docker run -it --rm \
  -v ~/.mcp.json:/home/appuser/.mcp.json:ro \
  -v ~/.env:/app/.env:ro \
  ghcr.io/viperjuice/mcp-gateway:latest

# Using Docker Compose
docker-compose up -d
```

Build locally:

```bash
docker build -t mcp-gateway .
docker run -it --rm mcp-gateway --help
```

## Health Monitoring

The gateway tracks health of all connections and pending requests.

### Check Health

```
gateway.health()
```

Returns server status, tool counts, and last refresh timestamp.

### Monitor Long-Running Requests

```
gateway.list_pending()
gateway.list_pending({ server: "playwright" })
```

Returns pending requests with elapsed time and heartbeat status.

### Cancel Stuck Requests

```
gateway.cancel({ request_id: "playwright::42" })
gateway.cancel({ request_id: "playwright::42", force: true })
```

Cancels a pending request. Use `force: true` to cancel even if the request has a recent heartbeat.

## MCP Resources & Prompts

The gateway proxies MCP resources and prompts from downstream servers.

### Resources

Resources are discoverable via the standard MCP protocol:

```
# List all resources (via MCP)
resources/list

# Read a resource
resources/read { uri: "file:///path/to/file" }
```

Resources are filtered by policy - see [Policy File](#policy-file).

### Prompts

Prompts are also proxied from downstream servers:

```
# List all prompts (via MCP)
prompts/list

# Get a prompt
prompts/get { name: "server::prompt-name" }
```

## Development

```bash
# Clone the repo
git clone https://github.com/ViperJuice/mcp-gateway
cd mcp-gateway

# Install with uv (recommended)
uv sync --all-extras

# Or with pip
pip install -e ".[dev]"

# Run tests
uv run pytest

# Run with debug logging
uv run mcp-gateway --debug
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=mcp_gateway

# Run specific test file
uv run pytest tests/test_policy.py -v
```

### Project Structure

```
mcp-gateway/
├── src/mcp_gateway/
│   ├── __init__.py
│   ├── __main__.py           # python -m mcp_gateway entry
│   ├── cli.py                # CLI commands (status, logs, init, refresh)
│   ├── server.py             # MCP server implementation
│   ├── types.py              # Pydantic models
│   ├── errors.py             # Error codes and exceptions
│   ├── config/
│   │   └── loader.py         # Config discovery (.mcp.json)
│   ├── client/
│   │   └── manager.py        # Downstream server connections (parallel, retry)
│   ├── policy/
│   │   └── policy.py         # Allow/deny lists for servers/tools/resources/prompts
│   ├── tools/
│   │   └── handlers.py       # Gateway tool implementations
│   ├── manifest/
│   │   ├── manifest.yaml     # Server manifest (25+ servers)
│   │   ├── loader.py         # Manifest loading
│   │   ├── matcher.py        # Capability matching
│   │   ├── installer.py      # Server provisioning
│   │   └── environment.py    # Platform/CLI detection
│   └── baml_client/          # BAML-generated LLM client (optional)
├── .github/
│   ├── dependabot.yml        # Dependency auto-updates
│   └── workflows/
│       ├── test.yml          # CI tests, lint, typecheck
│       ├── release.yml       # PyPI publishing
│       └── docker.yml        # Docker image builds
├── config/
│   └── .mcp.json.example     # Example config for Docker
├── tests/                    # 290+ tests, 65%+ coverage
├── Dockerfile                # Multi-stage build
├── docker-compose.yml
├── .env.example              # API key configuration template
├── pyproject.toml
└── README.md
```

## Troubleshooting

### Server Won't Connect

```bash
# Check server status
mcp-gateway status

# View detailed logs
mcp-gateway logs --level debug

# Try refreshing connections
mcp-gateway refresh --force
```

### Missing API Key

If a server requires an API key, set it in your environment or `.env` file:

```bash
# Check which key is needed
mcp-gateway status --server github

# Set the key
export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...

# Or add to .env
echo "GITHUB_PERSONAL_ACCESS_TOKEN=ghp_..." >> .env
```

### Tool Invocation Fails

```bash
# Check if tool exists
gateway.catalog_search({ query: "tool-name" })

# Get detailed schema
gateway.describe({ tool_id: "server::tool-name" })

# Check pending requests
gateway.list_pending()
```

### Connection Timeouts

The gateway uses exponential backoff retry (1s, 2s, 4s) for transient failures. If connections still fail:

```bash
# Increase log verbosity
mcp-gateway --debug

# Check if server process is running
mcp-gateway status --verbose
```

### Policy Blocking Access

If tools/resources/prompts are blocked by policy:

```yaml
# Check your policy file (~/.claude/gateway-policy.yaml)
# Remove items from denylist or add to allowlist

servers:
  allowlist: []    # Empty = allow all
  denylist: []

tools:
  denylist: []     # Remove blocking patterns

resources:
  denylist: []

prompts:
  denylist: []
```

## Architecture

```
┌─────────────────┐
│  Claude Code    │
│     TUI         │
└────────┬────────┘
         │ MCP (stdio)
         │ 9 tools only
         ▼
┌─────────────────┐
│  MCP Gateway    │
│  ┌───────────┐  │
│  │ Catalog   │  │  ◄─── Progressive disclosure
│  │ Registry  │  │
│  └───────────┘  │
│  ┌───────────┐  │
│  │ Manifest  │  │  ◄─── 25+ provisionable servers
│  │ + Matcher │  │
│  └───────────┘  │
│  ┌───────────┐  │
│  │ Policy    │  │  ◄─── Allow/deny, limits
│  │ Manager   │  │
│  └───────────┘  │
└────────┬────────┘
         │ MCP (stdio) × N
         │
    ┌────┴────┬────────┬────────┐
    ▼         ▼        ▼        ▼
┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐
│Playwrt│ │Context│ │GitHub │ │ ...   │
│(auto) │ │(auto) │ │(prov) │ │       │
└───────┘ └───────┘ └───────┘ └───────┘
```

## License

MIT

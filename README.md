# MCP Gateway

A meta-server for minimal Claude Code TUI context/tool bloat with progressive disclosure.

## Why This Exists

When Claude Code connects directly to multiple MCP servers (GitHub, Jira, DB, etc.), it loads **all** tool schemas into context. This causes:
- **Tool bloat**: Dozens of tool definitions consume context tokens
- **Static configuration**: Requires Claude Code restart to see new servers
- **No progressive disclosure**: Full schemas shown even when not needed

**MCP Gateway solves this** by acting as a single MCP server that Claude Code connects to. The gateway:
- Exposes only **5 stable meta-tools** (not the underlying tools)
- Dynamically discovers and connects to downstream MCP servers
- Returns **compact capability cards** first, detailed schemas only on request
- Enforces output size caps and optional secret redaction

## Quick Start

### Installation

```bash
# Clone and install
git clone <repo>
cd mcp-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Or install from PyPI (when published)
pip install mcp-gateway
```

### Configure Claude Code

Create/update your Claude Code MCP config to use ONLY the gateway:

**~/.claude/gateway-only.mcp.json**:
```json
{
  "mcpServers": {
    "gateway": {
      "command": "/path/to/mcp-gateway/.venv/bin/mcp-gateway",
      "args": []
    }
  }
}
```

Then configure your actual MCP servers in the standard locations (the gateway will discover them):

**~/.mcp.json** (user-level):
```json
{
  "mcpServers": {
    "github": {
      "command": "mcp-server-github",
      "args": []
    },
    "jira": {
      "command": "mcp-server-jira",
      "args": ["--project", "MYPROJ"]
    }
  }
}
```

**<project>/.mcp.json** (project-level, takes precedence):
```json
{
  "mcpServers": {
    "code-index": {
      "command": "python3",
      "args": ["/path/to/code-index-mcp/server.py"],
      "env": {
        "INDEX_PATH": "./.indexes"
      }
    }
  }
}
```

### Run Claude Code with Gateway

```bash
# Point Claude Code to gateway-only config
claude --mcp-config ~/.claude/gateway-only.mcp.json
```

## Gateway Tools

The gateway exposes exactly **5 tools**:

| Tool | Purpose |
|------|---------|
| `gateway.catalog_search` | Search available tools, returns compact capability cards |
| `gateway.describe` | Get detailed schema for a specific tool |
| `gateway.invoke` | Call a downstream tool with argument validation |
| `gateway.refresh` | Reload backend configs and reconnect |
| `gateway.health` | Get gateway and server health status |

### Example Workflow

```
You: "What tools can help me manage GitHub issues?"

Claude uses: gateway.catalog_search { query: "github issue" }

Returns:
{
  "results": [
    {
      "tool_id": "github::create_issue",
      "server": "github",
      "tool_name": "create_issue",
      "short_description": "Create a new issue in a GitHub repository",
      "tags": ["github", "git"],
      "availability": "online",
      "risk_hint": "high"
    },
    {
      "tool_id": "github::list_issues",
      ...
    }
  ],
  "total_available": 15,
  "truncated": false
}
```

```
You: "How do I use create_issue?"

Claude uses: gateway.describe { tool_id: "github::create_issue" }

Returns:
{
  "server": "github",
  "tool_name": "create_issue",
  "description": "Create a new issue in a GitHub repository",
  "args": [
    { "name": "title", "type": "string", "required": true, "short_description": "Issue title" },
    { "name": "body", "type": "string", "required": false, "short_description": "Issue body (markdown)" },
    { "name": "labels", "type": "array", "required": false, "short_description": "Labels to apply" }
  ],
  "safety_notes": ["This tool may modify data or have side effects."]
}
```

```
You: "Create an issue titled 'Fix login bug'"

Claude uses: gateway.invoke {
  tool_id: "github::create_issue",
  arguments: { title: "Fix login bug", body: "Login fails on mobile" }
}

Returns:
{
  "tool_id": "github::create_issue",
  "ok": true,
  "result": { "id": 123, "url": "https://github.com/..." },
  "truncated": false,
  "raw_size_estimate": 245
}
```

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

### CLI Options

```
mcp-gateway [OPTIONS]

OPTIONS:
  -h, --help              Show help
  -p, --project <path>    Project root for .mcp.json discovery
  -c, --config <path>     Custom MCP config file
  --policy <path>         Policy file (YAML or JSON)
  -l, --log-level <level> debug|info|warn|error (default: info)
  --debug                 Enable debug logging
  -q, --quiet             Only show errors

ENVIRONMENT:
  MCP_GATEWAY_CONFIG      Custom config file path
  MCP_GATEWAY_POLICY      Policy file path
  MCP_GATEWAY_LOG_LEVEL   Log level
```

## Development

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest -v

# Run with debug logging
mcp-gateway --debug
```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=mcp_gateway

# Run specific test file
pytest tests/test_policy.py -v
```

### Project Structure

```
mcp-gateway/
├── src/mcp_gateway/
│   ├── __init__.py
│   ├── __main__.py         # python -m mcp_gateway entry
│   ├── cli.py              # CLI argument parsing
│   ├── server.py           # MCP server
│   ├── types.py            # Pydantic models
│   ├── config/loader.py    # Config discovery
│   ├── client/manager.py   # Downstream connections
│   ├── policy/policy.py    # Allow/deny, limits
│   └── tools/handlers.py   # Gateway tools
├── tests/
├── examples/
├── pyproject.toml
└── README.md
```

## Architecture

```
┌─────────────────┐
│  Claude Code    │
│     TUI         │
└────────┬────────┘
         │ MCP (stdio)
         │ 5 tools only
         ▼
┌─────────────────┐
│  MCP Gateway    │
│  ┌───────────┐  │
│  │ Catalog   │  │  ◄─── Progressive disclosure
│  │ Registry  │  │
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
│GitHub │ │ Jira  │ │  DB   │ │ ...   │
│  MCP  │ │  MCP  │ │  MCP  │ │       │
└───────┘ └───────┘ └───────┘ └───────┘
```

## License

MIT

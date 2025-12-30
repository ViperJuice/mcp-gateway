# Contributing to PMCP

Thank you for your interest in contributing to PMCP (Progressive MCP)!

## Development Setup

```bash
# Clone the repository
git clone https://github.com/ViperJuice/pmcp
cd pmcp

# Install with uv (recommended)
uv sync --all-extras

# Or with pip
pip install -e ".[dev]"
```

## Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=pmcp --cov-report=term-missing

# Run specific test file
uv run pytest tests/test_policy.py -v

# Run integration tests (uses manifest auto-start servers)
uv run pytest tests/test_integration.py -v
```

## Adding a Server to the Manifest

The manifest (`src/pmcp/manifest/manifest.yaml`) contains 25+ MCP servers
that can be provisioned on-demand via `gateway.provision`.

### Steps to Add a New Server

1. Edit `src/pmcp/manifest/manifest.yaml`
2. Add an entry under `mcp_servers`:

```yaml
my-server:
  description: "Brief description of what this server does"
  keywords: [keyword1, keyword2, keyword3]
  install:
    mac: ["npx", "-y", "@scope/server-name"]
    linux: ["npx", "-y", "@scope/server-name"]
    windows: ["npx.cmd", "-y", "@scope/server-name"]
  command: "npx"
  args: ["-y", "@scope/server-name"]
  requires_api_key: true          # Set to false if no API key needed
  env_var: "MY_SERVER_API_KEY"    # Required if requires_api_key is true
  env_instructions: "Get your API key from https://..."
  auto_start: false               # Set to true for essential servers only
```

3. Add tests in `tests/test_manifest.py`
4. Run tests and submit a PR

### Auto-Start Servers

Only mark a server as `auto_start: true` if it:
- Provides essential functionality (like browser automation)
- Works without API keys (or with optional keys)
- Has minimal resource footprint

Currently, only Playwright and Context7 are auto-start servers.

## Code Style

- **Formatting**: Use `ruff format` for consistent formatting
- **Linting**: Use `ruff check` for linting
- **Type hints**: Required for all public functions (mypy checked in CI)
- **Tests**: Required for new features

```bash
# Format code
uv run ruff format .

# Check linting
uv run ruff check .

# Check types
uv run mypy src/pmcp
```

## Architecture Overview

See the [README Architecture section](README.md#architecture) for a visual overview.

### Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | MCP server implementation, tool handlers |
| `client/manager.py` | Downstream server connections (parallel, retry) |
| `config/loader.py` | Config discovery from `.mcp.json` files |
| `manifest/loader.py` | Server manifest loading |
| `manifest/installer.py` | On-demand server provisioning |
| `manifest/matcher.py` | Natural language capability matching |
| `policy/policy.py` | Allow/deny lists for servers, tools, resources, prompts |
| `tools/handlers.py` | Gateway tool implementations |

### Request Flow

1. Claude Code calls `gateway.invoke({ tool_id, arguments })`
2. Gateway parses tool_id to extract server name and tool name
3. Gateway checks policy for tool access
4. Gateway forwards request to downstream server
5. Response is truncated/redacted per policy
6. Result returned to Claude Code

## Pull Request Guidelines

1. **Create a branch** from `main`
2. **Write tests** for new functionality
3. **Run the full test suite** before submitting
4. **Update documentation** if adding features
5. **Keep PRs focused** - one feature or fix per PR

## Reporting Issues

Please include:
- Python version
- OS and version
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs (`pmcp logs --level debug`)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

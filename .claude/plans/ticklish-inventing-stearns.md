# Plan: Dynamic Capability Discovery & Provisioning

## Goal
Add environment-aware capability resolution with on-demand MCP server provisioning, CLI detection, and LLM-powered request matching.

## Features

### 1. Environment Detection
- Probe inherited environment on startup (PATH, installed CLIs)
- Cache detected CLIs for fast lookup
- Optional `gateway.sync_environment` for explicit environment info

### 2. Capability Request Tool
- New `gateway.request_capability` tool
- LLM-powered matching via BAML/Groq
- Priority: CLI → Active MCP → Dormant MCP → Not Available

### 3. MCP Server Manifest
- YAML manifest of installable MCP servers
- Platform-specific install commands (Mac, WSL/Linux)
- Keyword matching + LLM semantic matching
- API key requirements with .env.example

### 4. On-Demand Provisioning
- Install MCP server from manifest when requested
- Spin up and index tools
- Return new tools to Claude

---

## New Files

### `src/mcp_gateway/manifest/`
```
manifest/
├── __init__.py
├── loader.py          # Load and parse manifest.yaml
├── matcher.py         # BAML-powered request matching
├── installer.py       # Platform-specific installation
├── environment.py     # CLI detection and env probing
└── manifest.yaml      # Server definitions
```

### `baml_src/capability_match.baml`
```baml
class ManifestEntry {
  name string
  keywords string[]
  description string
}

class MatchResult {
  matched bool
  entry_name string @description("Name of matched manifest entry or empty")
  confidence float @description("0.0 to 1.0 confidence score")
  reasoning string @description("Why this matches or doesn't")
}

function MatchCapabilityRequest(
  query: string,
  manifest_entries: ManifestEntry[]
) -> MatchResult {
  client Groq
  prompt #"
    Match the user's capability request to the best manifest entry.

    User request: {{ query }}

    Available entries:
    {% for entry in manifest_entries %}
    - {{ entry.name }}: {{ entry.description }}
      Keywords: {{ entry.keywords | join(", ") }}
    {% endfor %}

    If no entry matches well, set matched=false.
    Confidence should reflect how well the request matches.

    {{ ctx.output_format }}
  "#
}
```

---

## Manifest Schema

### `src/mcp_gateway/manifest/manifest.yaml`

```yaml
version: "1.0"

# Environment detection
cli_alternatives:
  git:
    keywords: [git, version control, commits, branches, repository, clone]
    check_command: ["git", "--version"]
    help_command: ["git", "--help"]
    description: "Git version control CLI"
    prefer_mcp_for: [github issues, pull requests, github actions, github api]

  docker:
    keywords: [docker, container, image, dockerfile, compose]
    check_command: ["docker", "--version"]
    help_command: ["docker", "--help"]
    description: "Docker container CLI"

  kubectl:
    keywords: [kubernetes, k8s, pods, deployments, services, helm]
    check_command: ["kubectl", "version", "--client"]
    help_command: ["kubectl", "--help"]
    description: "Kubernetes CLI"

  node:
    keywords: [node, nodejs, npm, javascript, js]
    check_command: ["node", "--version"]
    help_command: ["node", "--help"]
    description: "Node.js runtime"

  python:
    keywords: [python, pip, python3]
    check_command: ["python3", "--version"]
    help_command: ["python3", "--help"]
    description: "Python interpreter"

  aws:
    keywords: [aws, amazon, s3, ec2, lambda, cloudformation]
    check_command: ["aws", "--version"]
    help_command: ["aws", "help"]
    description: "AWS CLI"

  gcloud:
    keywords: [gcp, google cloud, gcloud, bigquery]
    check_command: ["gcloud", "--version"]
    help_command: ["gcloud", "--help"]
    description: "Google Cloud CLI"

  az:
    keywords: [azure, az, microsoft cloud]
    check_command: ["az", "--version"]
    help_command: ["az", "--help"]
    description: "Azure CLI"

# MCP Servers (dormant until requested)
servers:
  # === No API Key Required ===

  playwright:
    description: "Browser automation - navigation, clicks, screenshots, DOM inspection"
    keywords: [browser, web, automation, playwright, screenshot, click, navigate, scrape]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-playwright"]
      wsl: ["npx", "-y", "@anthropic/mcp-playwright"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-playwright"]
    requires_api_key: false
    auto_start: true  # Start by default

  claude-in-chrome:
    description: "Chrome browser control - read pages, click, type, navigate, screenshots"
    keywords: [chrome, browser, web, automation, screenshot, click, navigate, tabs]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-claude-in-chrome"]
      wsl: ["npx", "-y", "@anthropic/mcp-claude-in-chrome"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-claude-in-chrome"]
    requires_api_key: false
    auto_start: true  # Start by default

  # === Bright Data MCPs (Web Scraping & Data) ===

  brightdata-scraper:
    description: "Web scraping at scale - scrape any website with rotating proxies and CAPTCHA solving"
    keywords: [scrape, scraping, web data, extract, crawl, brightdata, bright data, proxy]
    install:
      mac: ["npx", "-y", "@brightdata/mcp-scraper"]
      wsl: ["npx", "-y", "@brightdata/mcp-scraper"]
    command: "npx"
    args: ["-y", "@brightdata/mcp-scraper"]
    requires_api_key: true
    env_var: "BRIGHTDATA_API_KEY"
    env_instructions: "Get API key at https://brightdata.com/cp/api_tokens"
    auto_start: true  # Start by default

  brightdata-serp:
    description: "Search engine results - Google, Bing, DuckDuckGo SERP data"
    keywords: [search, serp, google, bing, search results, seo, brightdata]
    install:
      mac: ["npx", "-y", "@brightdata/mcp-serp"]
      wsl: ["npx", "-y", "@brightdata/mcp-serp"]
    command: "npx"
    args: ["-y", "@brightdata/mcp-serp"]
    requires_api_key: true
    env_var: "BRIGHTDATA_API_KEY"
    env_instructions: "Get API key at https://brightdata.com/cp/api_tokens"
    auto_start: true  # Start by default

  brightdata-unlocker:
    description: "Web Unlocker - access any website bypassing blocks and CAPTCHAs"
    keywords: [unblock, captcha, bypass, proxy, brightdata, anti-bot]
    install:
      mac: ["npx", "-y", "@brightdata/mcp-unlocker"]
      wsl: ["npx", "-y", "@brightdata/mcp-unlocker"]
    command: "npx"
    args: ["-y", "@brightdata/mcp-unlocker"]
    requires_api_key: true
    env_var: "BRIGHTDATA_API_KEY"
    env_instructions: "Get API key at https://brightdata.com/cp/api_tokens"

  brightdata-datasets:
    description: "Pre-built datasets - Amazon, LinkedIn, Google Maps, etc."
    keywords: [dataset, data, amazon, linkedin, google maps, ecommerce, brightdata]
    install:
      mac: ["npx", "-y", "@brightdata/mcp-datasets"]
      wsl: ["npx", "-y", "@brightdata/mcp-datasets"]
    command: "npx"
    args: ["-y", "@brightdata/mcp-datasets"]
    requires_api_key: true
    env_var: "BRIGHTDATA_API_KEY"
    env_instructions: "Get API key at https://brightdata.com/cp/api_tokens"

  filesystem:
    description: "File system operations - read, write, search files"
    keywords: [file, filesystem, read, write, directory, folder, fs]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-filesystem"]
      wsl: ["npx", "-y", "@anthropic/mcp-filesystem"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-filesystem", "/"]
    requires_api_key: false

  memory:
    description: "Persistent memory - store and retrieve information across sessions"
    keywords: [memory, remember, store, persist, recall, notes]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-memory"]
      wsl: ["npx", "-y", "@anthropic/mcp-memory"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-memory"]
    requires_api_key: false

  fetch:
    description: "HTTP requests - fetch web pages, APIs, download content"
    keywords: [http, fetch, request, api, web, download, curl]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-fetch"]
      wsl: ["npx", "-y", "@anthropic/mcp-fetch"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-fetch"]
    requires_api_key: false

  sqlite:
    description: "SQLite database operations"
    keywords: [sqlite, database, sql, query, db]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-sqlite"]
      wsl: ["npx", "-y", "@anthropic/mcp-sqlite"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-sqlite"]
    requires_api_key: false

  puppeteer:
    description: "Headless Chrome automation"
    keywords: [puppeteer, chrome, headless, browser, scraping]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-puppeteer"]
      wsl: ["npx", "-y", "@anthropic/mcp-puppeteer"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-puppeteer"]
    requires_api_key: false

  # === API Key Required ===

  github:
    description: "GitHub API - issues, PRs, repos, actions, code search"
    keywords: [github, issues, pull request, pr, repository, actions, workflows, code review]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-github"]
      wsl: ["npx", "-y", "@anthropic/mcp-github"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-github"]
    requires_api_key: true
    env_var: "GITHUB_TOKEN"
    env_instructions: "Create at https://github.com/settings/tokens with repo scope"

  slack:
    description: "Slack messaging - channels, DMs, search, notifications"
    keywords: [slack, messaging, chat, channels, dm, notifications, team]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-slack"]
      wsl: ["npx", "-y", "@anthropic/mcp-slack"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-slack"]
    requires_api_key: true
    env_var: "SLACK_TOKEN"
    env_instructions: "Create Slack app at https://api.slack.com/apps and get Bot Token"

  linear:
    description: "Linear issue tracking - issues, projects, cycles"
    keywords: [linear, issues, project management, tickets, bugs, tasks]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-linear"]
      wsl: ["npx", "-y", "@anthropic/mcp-linear"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-linear"]
    requires_api_key: true
    env_var: "LINEAR_API_KEY"
    env_instructions: "Create at https://linear.app/settings/api"

  notion:
    description: "Notion workspace - pages, databases, search"
    keywords: [notion, wiki, documentation, pages, databases, notes]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-notion"]
      wsl: ["npx", "-y", "@anthropic/mcp-notion"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-notion"]
    requires_api_key: true
    env_var: "NOTION_TOKEN"
    env_instructions: "Create integration at https://www.notion.so/my-integrations"

  postgres:
    description: "PostgreSQL database operations"
    keywords: [postgres, postgresql, database, sql, query]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-postgres"]
      wsl: ["npx", "-y", "@anthropic/mcp-postgres"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-postgres"]
    requires_api_key: true
    env_var: "POSTGRES_URL"
    env_instructions: "PostgreSQL connection string: postgresql://user:pass@host:5432/db"

  sentry:
    description: "Sentry error tracking - issues, events, releases"
    keywords: [sentry, errors, monitoring, debugging, exceptions, crashes]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-sentry"]
      wsl: ["npx", "-y", "@anthropic/mcp-sentry"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-sentry"]
    requires_api_key: true
    env_var: "SENTRY_AUTH_TOKEN"
    env_instructions: "Create at https://sentry.io/settings/account/api/auth-tokens/"

  google-drive:
    description: "Google Drive - files, folders, search, sharing"
    keywords: [google drive, gdrive, docs, sheets, files, cloud storage]
    install:
      mac: ["npx", "-y", "@anthropic/mcp-google-drive"]
      wsl: ["npx", "-y", "@anthropic/mcp-google-drive"]
    command: "npx"
    args: ["-y", "@anthropic/mcp-google-drive"]
    requires_api_key: true
    env_var: "GOOGLE_CREDENTIALS"
    env_instructions: "Create OAuth credentials at https://console.cloud.google.com/apis/credentials"

  context7:
    description: "Library documentation lookup - up-to-date docs for any package"
    keywords: [documentation, docs, library, package, api reference, context7]
    install:
      mac: ["npx", "-y", "@upstash/context7-mcp"]
      wsl: ["npx", "-y", "@upstash/context7-mcp"]
    command: "npx"
    args: ["-y", "@upstash/context7-mcp"]
    requires_api_key: false
    auto_start: true

# Discovery queue for unmatched requests
discovery_queue_path: ".mcp-gateway/discovery_queue.json"
```

---

## .env.example

```bash
# MCP Gateway Environment Variables
# Copy to .env and fill in your API keys

# === Required for LLM-powered matching ===
GROQ_API_KEY=gsk_your_groq_api_key_here

# === Default Auto-Start Servers (API keys required) ===

# Bright Data - https://brightdata.com/cp/api_tokens
# Required for: brightdata-scraper, brightdata-serp, brightdata-unlocker, brightdata-datasets
BRIGHTDATA_API_KEY=your_brightdata_api_key_here

# === Optional MCP Server API Keys ===
# Uncomment and fill in as needed

# GitHub - https://github.com/settings/tokens (repo scope)
#GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Slack - https://api.slack.com/apps (Bot Token)
#SLACK_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx

# Linear - https://linear.app/settings/api
#LINEAR_API_KEY=lin_api_xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Notion - https://www.notion.so/my-integrations
#NOTION_TOKEN=secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# PostgreSQL - Connection string
#POSTGRES_URL=postgresql://user:password@localhost:5432/database

# Sentry - https://sentry.io/settings/account/api/auth-tokens/
#SENTRY_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Google Drive - OAuth credentials JSON path
#GOOGLE_CREDENTIALS=/path/to/credentials.json
```

---

## New Types

### `src/mcp_gateway/types.py` additions

```python
class CapabilityRequestInput(BaseModel):
    """Input for gateway.request_capability."""
    query: str = Field(min_length=1, description="Natural language capability request")
    available_clis: list[str] | None = Field(default=None, description="CLIs Claude knows are available")

class CapabilityResolution(BaseModel):
    """Result of capability resolution."""
    status: Literal["use_cli", "available", "provisioned", "needs_api_key", "not_available"]

    # For use_cli
    cli: str | None = None
    cli_path: str | None = None
    cli_help: str | None = None
    cli_examples: list[str] | None = None

    # For available/provisioned
    server: str | None = None
    new_tools: list[CapabilityCard] | None = None

    # For needs_api_key
    env_var: str | None = None
    env_path: str | None = None
    env_instructions: str | None = None

    # For not_available
    logged_for_discovery: bool = False

    message: str

class EnvironmentInfo(BaseModel):
    """Environment information from Claude or detected."""
    path: str | None = None
    cwd: str | None = None
    platform: Literal["mac", "wsl", "linux", "windows"] | None = None
    detected_clis: list[str] = Field(default_factory=list)
```

---

## New Gateway Tools

### `gateway.request_capability`
```python
Tool(
    name="gateway.request_capability",
    description=(
        "Request a capability that may not be currently available. "
        "The gateway will check installed CLIs, active servers, and dormant servers. "
        "May install and start a new MCP server if available in manifest."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language description of needed capability"
            },
            "available_clis": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: CLIs you know are available in the environment"
            }
        },
        "required": ["query"]
    }
)
```

### `gateway.sync_environment`
```python
Tool(
    name="gateway.sync_environment",
    description=(
        "Sync environment information with the gateway. "
        "Call this if the gateway's environment detection seems incorrect."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "platform": {
                "type": "string",
                "enum": ["mac", "wsl", "linux", "windows"]
            },
            "detected_clis": {
                "type": "array",
                "items": {"type": "string"},
                "description": "CLIs confirmed to be installed"
            }
        }
    }
)
```

---

## Implementation Flow

### Startup
```python
async def initialize(self):
    # 1. Detect platform
    self._platform = detect_platform()  # mac, wsl, linux

    # 2. Load manifest
    self._manifest = load_manifest()

    # 3. Probe environment for CLIs
    self._detected_clis = await probe_clis(self._manifest.cli_alternatives)

    # 4. Connect to auto_start servers (playwright, context7)
    await self._connect_auto_start_servers()

    # 5. Generate capability summary (includes detected CLIs)
    self._capability_summary = await generate_capability_summary(
        tools=self._client_manager.get_all_tools(),
        detected_clis=self._detected_clis,
    )
```

### request_capability Flow
```python
async def request_capability(self, input_data: dict) -> CapabilityResolution:
    parsed = CapabilityRequestInput.model_validate(input_data)
    query = parsed.query

    # 1. Check if active MCP server already has matching tools
    if tools := self._search_active_tools(query):
        return CapabilityResolution(
            status="available",
            message=f"Tools already available",
            new_tools=tools
        )

    # 2. Check for matching CLI
    if cli := self._match_cli(query):
        if cli.name in self._detected_clis:
            help_output = await self._get_cli_help(cli)
            return CapabilityResolution(
                status="use_cli",
                cli=cli.name,
                cli_path=self._cli_paths.get(cli.name),
                cli_help=help_output,
                cli_examples=cli.examples,
                message=f"Use {cli.name} CLI via Bash tool"
            )

    # 3. Use BAML/Groq to match against manifest
    match = await self._match_manifest(query)

    if not match.matched:
        # Log for discovery
        await self._log_discovery_request(query)
        return CapabilityResolution(
            status="not_available",
            logged_for_discovery=True,
            message="No matching capability found. Request logged for future discovery."
        )

    server_config = self._manifest.servers[match.entry_name]

    # 4. Check API key if required
    if server_config.requires_api_key:
        env_var = server_config.env_var
        if not os.environ.get(env_var):
            return CapabilityResolution(
                status="needs_api_key",
                server=match.entry_name,
                env_var=env_var,
                env_path=str(Path.cwd() / ".env"),
                env_instructions=server_config.env_instructions,
                message=f"Capability available but requires {env_var} to be set"
            )

    # 5. Install and start server
    try:
        await self._install_server(match.entry_name)
        await self._start_server(match.entry_name)

        new_tools = self._client_manager.get_tools_for_server(match.entry_name)

        return CapabilityResolution(
            status="provisioned",
            server=match.entry_name,
            new_tools=[self._to_capability_card(t) for t in new_tools],
            message=f"Started {match.entry_name} MCP server with {len(new_tools)} tools"
        )
    except Exception as e:
        return CapabilityResolution(
            status="not_available",
            message=f"Failed to provision {match.entry_name}: {e}"
        )
```

---

## Files to Create/Modify

| File | Action |
|------|--------|
| `src/mcp_gateway/manifest/__init__.py` | Create |
| `src/mcp_gateway/manifest/loader.py` | Create |
| `src/mcp_gateway/manifest/matcher.py` | Create |
| `src/mcp_gateway/manifest/installer.py` | Create |
| `src/mcp_gateway/manifest/environment.py` | Create |
| `src/mcp_gateway/manifest/manifest.yaml` | Create |
| `baml_src/capability_match.baml` | Create |
| `.env.example` | Create |
| `src/mcp_gateway/types.py` | Add new types |
| `src/mcp_gateway/tools/handlers.py` | Add new tools |
| `src/mcp_gateway/server.py` | Add manifest loading, env detection |
| `tests/test_manifest.py` | Create |
| `tests/test_capability_request.py` | Create |

---

## Updated Capability Summary

The L1 handshake summary will now include detected CLIs:

```
MCP Gateway capabilities:
• Browser Automation (playwright): Navigate, click, screenshot
• Documentation (context7): Library docs lookup

Detected CLIs (use via Bash): git, docker, node, python, aws

Use gateway.request_capability to discover more capabilities.
Use gateway.catalog_search to explore available tools.
```

---

## Testing Plan

1. **Unit tests**: CLI detection, manifest loading, BAML matching
2. **Integration tests**:
   - Request capability → CLI resolution
   - Request capability → Server provisioning
   - Request capability → API key needed response
3. **E2E test**: Full flow with Groq API

---

## Execution Order

1. Create manifest module structure
2. Implement environment.py (CLI detection)
3. Implement loader.py (manifest parsing)
4. Add BAML capability_match.baml + regenerate client
5. Implement matcher.py (BAML-powered matching)
6. Implement installer.py (platform-specific install)
7. Add new types to types.py
8. Add new tools to handlers.py
9. Update server.py initialization
10. Create .env.example
11. Add tests
12. Run full test suite

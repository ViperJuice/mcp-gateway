# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2025-12-30

### Added

- **Code Execution Guidance System**: Multi-layered progressive disclosure to encourage models to use code patterns
  - **L0 (MCP Instructions)**: Brief philosophy about code execution (~30 tokens)
  - **L1 (Capability Cards)**: Ultra-terse code pattern hints during search (~8-12 tokens/card)
  - **L2 (Schema Cards)**: Optional code examples in tool details (~40-80 tokens/schema, opt-in)
  - **L3 (Methodology Resource)**: Full code execution guide (lazy-loaded via resource)

- **Guidance Configuration**: `~/.claude/gateway-guidance.yaml` for customization
  - Three levels: `off`, `minimal` (default), `standard`
  - Token budget estimation (~200 tokens in minimal mode)
  - Per-layer control for fine-grained configuration

- **Code Pattern Hints**: Keyword-based matching for common patterns
  - `loop` - For batch operations (navigate, create, update, list)
  - `filter` - For search/query operations that return many results
  - `if/else` - For conditional logic based on tool results
  - `try/catch` - For error-prone operations (invoke, execute, provision)
  - `poll` - For status checking and waiting operations

- **Code Snippet Templates**: 25+ static examples for common tools
  - Playwright browser automation
  - File system operations
  - GitHub API calls
  - Database queries
  - Optional LLM-generated examples via BAML for dynamic tools

- **CLI Commands**: New `pmcp guidance` command
  - `pmcp guidance` - Show current configuration and status
  - `pmcp guidance --show-budget` - Display token cost estimates

- **Comprehensive Tests**: 48 new test cases for guidance system
  - Configuration loading and validation
  - Token budget estimation
  - Pattern hint matching
  - Code snippet template loading
  - 86% test coverage for guidance modules

### Changed

- **MCP Server Instructions**: Updated to include code execution philosophy
- **Summary Templates**: Enhanced with progressive disclosure messaging
- **BAML Prompts**: Updated to emphasize code execution patterns

### Technical Details

- Token budget optimized: ~200 tokens in minimal mode (80% reduction vs naive approach)
- Hybrid static/LLM approach: Static templates for manifest tools, LLM generation for dynamic tools
- Graceful degradation: System works without BAML or missing template files
- No breaking changes: All existing functionality preserved

## [1.0.0] - 2025-12-29

### Added

- **MCP Gateway Server**: Meta-server that aggregates multiple MCP servers behind a single connection
- **Progressive Tool Discovery**: 9 gateway tools instead of exposing all downstream tools directly
  - `gateway.catalog_search` - Search available tools with filters
  - `gateway.describe` - Get detailed tool schemas
  - `gateway.invoke` - Call tools on downstream servers
  - `gateway.health` - Check server status
  - `gateway.refresh` - Reload server configurations
  - `gateway.request_capability` - Natural language capability matching
  - `gateway.sync_environment` - Detect available CLIs
  - `gateway.provision` - Install MCP servers on demand
  - `gateway.provision_status` - Track installation progress

- **BAML-Powered Capability Matching**: Intelligent matching of user requests to available CLIs or MCP servers
- **CLI Preference**: Prefers installed CLIs (git, docker, etc.) over MCP servers when appropriate
- **Dynamic Server Provisioning**: Install and connect to MCP servers at runtime via npx/uvx
- **Process Handoff**: Seamless adoption of npx-started servers into the gateway
- **Auto-Start Servers**: Playwright and Context7 servers start automatically
- **Server Manifest**: Curated list of 25+ MCP servers with install instructions
- **Policy Management**: Server/tool allowlists, denylists, and output processing

### Technical Details

- Pure Python implementation using `asyncio`
- JSON-RPC over stdio for MCP communication
- Supports both npm (npx) and Python (uvx) MCP servers
- Environment variable support for API keys via `.env` files

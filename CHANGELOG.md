# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

"""Type definitions for MCP Gateway using Pydantic."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# === Config Types ===


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] | None = None
    # HTTP transport (optional)
    url: str | None = None
    headers: dict[str, str] | None = None


class McpConfigFile(BaseModel):
    """Structure of .mcp.json files."""

    mcpServers: dict[str, McpServerConfig] = Field(default_factory=dict)


class ResolvedServerConfig(BaseModel):
    """A server config resolved from a config file."""

    name: str
    source: Literal["project", "user", "custom"]
    config: McpServerConfig


# === Registry Types ===


class RiskHint(str, Enum):
    """Risk level hint for tools."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class ServerStatusEnum(str, Enum):
    """Server connection status."""

    ONLINE = "online"
    OFFLINE = "offline"
    CONNECTING = "connecting"
    ERROR = "error"


class ToolInfo(BaseModel):
    """Internal tool information."""

    tool_id: str  # Normalized: server_name::tool_name
    server_name: str
    tool_name: str
    description: str
    short_description: str  # Truncated for catalog
    input_schema: dict[str, Any]
    tags: list[str]
    risk_hint: RiskHint


class ServerStatus(BaseModel):
    """Status of a connected server."""

    name: str
    status: ServerStatusEnum
    tool_count: int
    last_error: str | None = None
    last_connected_at: float | None = None


# === Gateway Tool Input/Output Types ===


class CatalogFilters(BaseModel):
    """Filters for catalog search."""

    server: str | None = None
    tags: list[str] | None = None
    risk_max: Literal["low", "medium", "high"] | None = None


class CatalogSearchInput(BaseModel):
    """Input for gateway.catalog_search."""

    query: str | None = None
    filters: CatalogFilters | None = None
    limit: int = Field(default=20, ge=1, le=100)
    include_offline: bool = False


class CapabilityCard(BaseModel):
    """Compact tool representation for catalog results."""

    tool_id: str
    server: str
    tool_name: str
    short_description: str
    tags: list[str]
    availability: Literal["online", "offline"]
    risk_hint: str


class CatalogSearchOutput(BaseModel):
    """Output for gateway.catalog_search."""

    results: list[CapabilityCard]
    total_available: int
    truncated: bool


class DescribeInput(BaseModel):
    """Input for gateway.describe."""

    tool_id: str = Field(min_length=1)


class ArgInfo(BaseModel):
    """Argument information for schema card."""

    name: str
    type: str
    required: bool
    short_description: str
    examples: list[Any] | None = None


class InvokeTemplate(BaseModel):
    """Template for invoking a tool via gateway.invoke."""

    tool_id: str
    arguments: dict[str, str]  # arg_name -> description placeholder


class SchemaCard(BaseModel):
    """Detailed tool information for describe output."""

    server: str
    tool_name: str
    description: str
    args: list[ArgInfo]
    constraints: list[str] | None = None
    safety_notes: list[str] | None = None
    # Direct invocation template
    invoke_as: str = "gateway.invoke"
    invoke_template: InvokeTemplate | None = None


class InvokeOptions(BaseModel):
    """Options for tool invocation."""

    timeout_ms: int = Field(default=30000, ge=1000, le=300000)
    max_output_chars: int | None = Field(default=None, ge=100, le=100000)
    redact_secrets: bool = False


class InvokeInput(BaseModel):
    """Input for gateway.invoke."""

    tool_id: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    options: InvokeOptions | None = None


class InvokeOutput(BaseModel):
    """Output for gateway.invoke."""

    tool_id: str
    ok: bool
    result: Any | None = None
    truncated: bool
    summary: str | None = None
    raw_size_estimate: int
    errors: list[str] | None = None


class RefreshInput(BaseModel):
    """Input for gateway.refresh."""

    source: Literal["claude_config", "custom"] | None = None
    reason: str | None = None


class RefreshOutput(BaseModel):
    """Output for gateway.refresh."""

    ok: bool
    servers_seen: int
    servers_online: int
    tools_indexed: int
    revision_id: str
    errors: list[str] | None = None


class ServerHealthInfo(BaseModel):
    """Server info in health output."""

    name: str
    status: str
    tool_count: int


class HealthOutput(BaseModel):
    """Output for gateway.health."""

    revision_id: str
    servers: list[ServerHealthInfo]
    last_refresh_ts: float


# === Policy Types ===


class ServerPolicy(BaseModel):
    """Server allow/deny policy."""

    allowlist: list[str] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)


class ToolPolicy(BaseModel):
    """Tool allow/deny policy."""

    allowlist: list[str] = Field(default_factory=list)  # Glob patterns
    denylist: list[str] = Field(default_factory=list)  # Glob patterns


class LimitsPolicy(BaseModel):
    """Resource limits policy."""

    max_tools_per_server: int = 100
    max_output_bytes: int = 50000  # 50KB
    max_output_tokens: int = 4000


class RedactionPolicy(BaseModel):
    """Secret redaction policy."""

    patterns: list[str] = Field(default_factory=list)  # Regex patterns


class GatewayPolicy(BaseModel):
    """Complete gateway policy."""

    servers: ServerPolicy = Field(default_factory=ServerPolicy)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    limits: LimitsPolicy = Field(default_factory=LimitsPolicy)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)

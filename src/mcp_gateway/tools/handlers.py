"""Gateway Tool Implementations."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.types import Tool

from mcp_gateway.client.manager import ClientManager
from mcp_gateway.config.loader import load_configs, parse_tool_id
from mcp_gateway.policy.policy import PolicyManager
from mcp_gateway.types import (
    ArgInfo,
    CapabilityCard,
    CatalogSearchInput,
    CatalogSearchOutput,
    DescribeInput,
    HealthOutput,
    InvokeInput,
    InvokeOutput,
    RefreshInput,
    RefreshOutput,
    SchemaCard,
    ServerHealthInfo,
)

logger = logging.getLogger(__name__)

# Risk level ordering for filtering
RISK_ORDER = {"low": 1, "medium": 2, "high": 3, "unknown": 4}


def get_gateway_tool_definitions() -> list[Tool]:
    """Get MCP tool definitions for the gateway."""
    return [
        Tool(
            name="gateway.catalog_search",
            description=(
                "Search for available tools across all connected MCP servers. "
                "Returns compact capability cards without full schemas. "
                "Use filters to narrow results by server, tags, or risk level."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to match against tool names, descriptions, and tags",
                    },
                    "filters": {
                        "type": "object",
                        "properties": {
                            "server": {
                                "type": "string",
                                "description": "Filter to tools from a specific server",
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Filter to tools with any of these tags",
                            },
                            "risk_max": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                                "description": "Maximum risk level to include",
                            },
                        },
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                        "description": "Maximum number of results to return",
                    },
                    "include_offline": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include tools from offline servers",
                    },
                },
            },
        ),
        Tool(
            name="gateway.describe",
            description=(
                "Get detailed information about a specific tool, including its arguments and constraints. "
                "Use this before invoking a tool to understand its requirements."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": 'The tool ID in format "server_name::tool_name"',
                    },
                },
                "required": ["tool_id"],
            },
        ),
        Tool(
            name="gateway.invoke",
            description=(
                "Invoke a tool on a downstream MCP server. "
                "Arguments are validated against the tool schema before execution. "
                "Output is automatically truncated if too large."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": 'The tool ID in format "server_name::tool_name"',
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments to pass to the tool (must match tool schema)",
                    },
                    "options": {
                        "type": "object",
                        "properties": {
                            "timeout_ms": {
                                "type": "integer",
                                "minimum": 1000,
                                "maximum": 300000,
                                "default": 30000,
                                "description": "Timeout in milliseconds",
                            },
                            "max_output_chars": {
                                "type": "integer",
                                "minimum": 100,
                                "maximum": 100000,
                                "description": "Maximum output characters (truncated if exceeded)",
                            },
                            "redact_secrets": {
                                "type": "boolean",
                                "default": False,
                                "description": "Redact detected secrets from output",
                            },
                        },
                    },
                },
                "required": ["tool_id"],
            },
        ),
        Tool(
            name="gateway.refresh",
            description=(
                "Reload backend MCP server configurations and reconnect. "
                "Use this when new MCP servers have been configured or to recover from connection errors."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["claude_config", "custom"],
                        "description": "Config source to reload from",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for refresh (for logging)",
                    },
                },
            },
        ),
        Tool(
            name="gateway.health",
            description=(
                "Get the health status of the gateway and all connected MCP servers. "
                "Shows server status, tool counts, and last refresh time."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


class GatewayTools:
    """Gateway tool handler implementations."""

    def __init__(
        self,
        client_manager: ClientManager,
        policy_manager: PolicyManager,
        project_root: Path | None = None,
        custom_config_path: Path | None = None,
    ) -> None:
        self._client_manager = client_manager
        self._policy_manager = policy_manager
        self._project_root = project_root
        self._custom_config_path = custom_config_path

    async def catalog_search(self, input_data: dict[str, Any]) -> CatalogSearchOutput:
        """gateway.catalog_search - Search for available tools."""
        parsed = CatalogSearchInput.model_validate(input_data)

        tools = self._client_manager.get_all_tools()
        total_available = len(tools)

        # Filter by policy
        tools = [t for t in tools if self._policy_manager.is_tool_allowed(t.tool_id)]

        # Filter by server online status
        if not parsed.include_offline:
            tools = [t for t in tools if self._client_manager.is_server_online(t.server_name)]

        # Filter by server name
        if parsed.filters and parsed.filters.server:
            tools = [t for t in tools if t.server_name == parsed.filters.server]

        # Filter by tags
        if parsed.filters and parsed.filters.tags:
            filter_tags = [tag.lower() for tag in parsed.filters.tags]
            tools = [t for t in tools if any(tag in t.tags for tag in filter_tags)]

        # Filter by max risk level
        if parsed.filters and parsed.filters.risk_max:
            max_risk = RISK_ORDER.get(parsed.filters.risk_max, 4)
            tools = [t for t in tools if RISK_ORDER.get(t.risk_hint.value, 4) <= max_risk]

        # Text search (if query provided)
        if parsed.query:
            query_lower = parsed.query.lower()
            tools = [
                t
                for t in tools
                if query_lower in t.tool_name.lower()
                or query_lower in t.short_description.lower()
                or any(query_lower in tag for tag in t.tags)
            ]

        # Sort by relevance (if query) or alphabetically
        if parsed.query:
            query_lower = parsed.query.lower()

            def sort_key(t: Any) -> tuple[int, int, str]:
                exact = t.tool_name.lower() == query_lower
                starts = t.tool_name.lower().startswith(query_lower)
                return (0 if exact else 1, 0 if starts else 1, t.tool_name)

            tools.sort(key=sort_key)
        else:
            tools.sort(key=lambda t: t.tool_name)

        # Apply limit
        truncated = len(tools) > parsed.limit
        tools = tools[: parsed.limit]

        # Convert to capability cards
        results = [
            CapabilityCard(
                tool_id=t.tool_id,
                server=t.server_name,
                tool_name=t.tool_name,
                short_description=t.short_description,
                tags=t.tags,
                availability="online" if self._client_manager.is_server_online(t.server_name) else "offline",
                risk_hint=t.risk_hint.value,
            )
            for t in tools
        ]

        return CatalogSearchOutput(
            results=results,
            total_available=total_available,
            truncated=truncated,
        )

    async def describe(self, input_data: dict[str, Any]) -> SchemaCard:
        """gateway.describe - Get detailed info about a tool."""
        parsed = DescribeInput.model_validate(input_data)

        tool_info = self._client_manager.get_tool(parsed.tool_id)
        if not tool_info:
            raise ValueError(f"Tool not found: {parsed.tool_id}")

        if not self._policy_manager.is_tool_allowed(parsed.tool_id):
            raise ValueError(f"Tool is not allowed by policy: {parsed.tool_id}")

        # Extract args from schema
        args: list[ArgInfo] = []
        schema = tool_info.input_schema
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for name, prop in properties.items():
            prop_type = prop.get("type", "unknown")
            description = prop.get("description", "")

            args.append(
                ArgInfo(
                    name=name,
                    type=str(prop_type),
                    required=name in required,
                    short_description=description[:200] if description else "",
                    examples=prop.get("examples"),
                )
            )

        # Generate safety notes based on risk
        safety_notes: list[str] = []
        if tool_info.risk_hint.value == "high":
            safety_notes.append("This tool may modify data or have side effects.")

        return SchemaCard(
            server=tool_info.server_name,
            tool_name=tool_info.tool_name,
            description=tool_info.description,
            args=args,
            safety_notes=safety_notes if safety_notes else None,
        )

    async def invoke(self, input_data: dict[str, Any]) -> InvokeOutput:
        """gateway.invoke - Call a downstream tool."""
        parsed = InvokeInput.model_validate(input_data)

        tool_info = self._client_manager.get_tool(parsed.tool_id)
        if not tool_info:
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[f"Tool not found: {parsed.tool_id}"],
            )

        if not self._policy_manager.is_tool_allowed(parsed.tool_id):
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[f"Tool is not allowed by policy: {parsed.tool_id}"],
            )

        # Call the tool
        try:
            timeout_ms = parsed.options.timeout_ms if parsed.options else 30000
            result = await self._client_manager.call_tool(
                parsed.tool_id, parsed.arguments, timeout_ms
            )

            # Process output (truncate, redact)
            max_bytes = None
            if parsed.options and parsed.options.max_output_chars:
                max_bytes = parsed.options.max_output_chars * 4  # Rough bytes estimate

            redact = parsed.options.redact_secrets if parsed.options else False

            processed = self._policy_manager.process_output(
                result, redact=redact, max_bytes=max_bytes
            )

            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=True,
                result=processed["result"],
                truncated=processed["truncated"],
                summary=processed["summary"],
                raw_size_estimate=processed["raw_size"],
            )

        except Exception as e:
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[str(e)],
            )

    async def refresh(self, input_data: dict[str, Any]) -> RefreshOutput:
        """gateway.refresh - Reload backend configs and reconnect."""
        parsed = RefreshInput.model_validate(input_data)

        logger.info(f"Refresh requested: {parsed.reason or 'manual refresh'}")

        try:
            # Reload configs
            configs = load_configs(
                project_root=self._project_root,
                custom_config_path=self._custom_config_path,
            )

            # Filter by policy
            allowed_configs = [
                c for c in configs if self._policy_manager.is_server_allowed(c.name)
            ]

            # Reconnect
            errors = await self._client_manager.refresh(allowed_configs)

            revision_id, _ = self._client_manager.get_registry_meta()
            statuses = self._client_manager.get_all_server_statuses()

            return RefreshOutput(
                ok=len(errors) == 0,
                servers_seen=len(configs),
                servers_online=sum(1 for s in statuses if s.status.value == "online"),
                tools_indexed=len(self._client_manager.get_all_tools()),
                revision_id=revision_id,
                errors=errors if errors else None,
            )

        except Exception as e:
            return RefreshOutput(
                ok=False,
                servers_seen=0,
                servers_online=0,
                tools_indexed=0,
                revision_id="error",
                errors=[str(e)],
            )

    async def health(self) -> HealthOutput:
        """gateway.health - Get gateway health status."""
        revision_id, last_refresh_ts = self._client_manager.get_registry_meta()
        statuses = self._client_manager.get_all_server_statuses()

        return HealthOutput(
            revision_id=revision_id,
            servers=[
                ServerHealthInfo(
                    name=s.name,
                    status=s.status.value,
                    tool_count=s.tool_count,
                )
                for s in statuses
            ],
            last_refresh_ts=last_refresh_ts,
        )

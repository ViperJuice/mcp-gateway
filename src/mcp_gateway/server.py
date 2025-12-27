"""MCP Gateway Server."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from mcp_gateway.client.manager import ClientManager
from mcp_gateway.config.loader import load_configs
from mcp_gateway.policy.policy import PolicyManager
from mcp_gateway.tools.handlers import GatewayTools, get_gateway_tool_definitions

logger = logging.getLogger(__name__)


class GatewayServer:
    """MCP Gateway Server."""

    def __init__(
        self,
        project_root: Path | None = None,
        custom_config_path: Path | None = None,
        policy_path: Path | None = None,
    ) -> None:
        self._project_root = project_root
        self._custom_config_path = custom_config_path

        # Initialize policy manager
        self._policy_manager = PolicyManager(policy_path)

        # Initialize client manager
        self._client_manager = ClientManager(
            max_tools_per_server=self._policy_manager.get_max_tools_per_server()
        )

        # Initialize gateway tools handler
        self._gateway_tools = GatewayTools(
            client_manager=self._client_manager,
            policy_manager=self._policy_manager,
            project_root=project_root,
            custom_config_path=custom_config_path,
        )

        # Create MCP server
        self._server = Server("mcp-gateway")
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        """Set up MCP request handlers."""

        @self._server.list_tools()
        async def list_tools() -> list[Tool]:
            return get_gateway_tool_definitions()

        @self._server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            try:
                result: Any

                if name == "gateway.catalog_search":
                    result = await self._gateway_tools.catalog_search(arguments)
                elif name == "gateway.describe":
                    result = await self._gateway_tools.describe(arguments)
                elif name == "gateway.invoke":
                    result = await self._gateway_tools.invoke(arguments)
                elif name == "gateway.refresh":
                    result = await self._gateway_tools.refresh(arguments)
                elif name == "gateway.health":
                    result = await self._gateway_tools.health()
                else:
                    raise ValueError(f"Unknown tool: {name}")

                # Convert Pydantic model to dict if needed
                if hasattr(result, "model_dump"):
                    result = result.model_dump()

                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            except Exception as e:
                logger.error(f"Tool execution error: {e}")
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": True, "message": str(e)}),
                    )
                ]

    async def initialize(self) -> None:
        """Initialize connections to downstream servers."""
        logger.info("Initializing MCP Gateway...")

        # Load configs
        configs = load_configs(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )

        # Filter by policy
        allowed_configs = [
            c for c in configs if self._policy_manager.is_server_allowed(c.name)
        ]

        if not allowed_configs:
            logger.warning("No MCP servers configured or all blocked by policy")
        else:
            logger.info(f"Found {len(allowed_configs)} allowed server configs")

        # Connect to all servers
        errors = await self._client_manager.connect_all(allowed_configs)

        if errors:
            logger.warning(f"Some servers failed to connect: {len(errors)} errors")

        statuses = self._client_manager.get_all_server_statuses()
        online = sum(1 for s in statuses if s.status.value == "online")
        total_tools = len(self._client_manager.get_all_tools())

        logger.info(
            f"Gateway initialized: {online}/{len(statuses)} servers online, {total_tools} tools indexed"
        )

    async def run(self) -> None:
        """Run the MCP server (stdio transport)."""
        from mcp.server.stdio import stdio_server

        await self.initialize()

        async with stdio_server() as (read_stream, write_stream):
            logger.info("MCP Gateway server started")
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )

    async def shutdown(self) -> None:
        """Shutdown the server."""
        logger.info("Shutting down MCP Gateway...")
        await self._client_manager.disconnect_all()
        logger.info("MCP Gateway shut down")

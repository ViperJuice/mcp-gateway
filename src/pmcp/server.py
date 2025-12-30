"""MCP Gateway Server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)
from pydantic import AnyUrl

from pmcp.client.manager import ClientManager
from pmcp.config.loader import load_configs, manifest_server_to_config
from pmcp.identity import filter_self_references, acquire_singleton_lock, release_singleton_lock
from pmcp.manifest.loader import load_manifest
from pmcp.manifest.refresher import (
    get_cache_path,
    load_descriptions_cache,
    refresh_all,
)
from pmcp.policy.policy import PolicyManager
from pmcp.summary import generate_capability_summary
from pmcp.tools.handlers import GatewayTools, get_gateway_tool_definitions
from pmcp.types import DescriptionsCache

logger = logging.getLogger(__name__)


class GatewayServer:
    """MCP Gateway Server."""

    def __init__(
        self,
        project_root: Path | None = None,
        custom_config_path: Path | None = None,
        policy_path: Path | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._project_root = project_root
        self._custom_config_path = custom_config_path
        self._cache_dir = cache_dir or Path(".mcp-gateway")

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

        # Server will be created after initialization with capability summary
        self._server: Server | None = None
        self._capability_summary: str = ""

        # Pre-built descriptions cache
        self._descriptions_cache: DescriptionsCache | None = None

    def _create_server(self, instructions: str | None = None) -> None:
        """Create the MCP server with optional capability instructions."""
        self._server = Server("mcp-gateway", instructions=instructions)
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        """Set up MCP request handlers."""
        if self._server is None:
            raise RuntimeError("Server not initialized")

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
                elif name == "gateway.request_capability":
                    result = await self._gateway_tools.request_capability(arguments)
                elif name == "gateway.sync_environment":
                    result = await self._gateway_tools.sync_environment(arguments)
                elif name == "gateway.provision":
                    result = await self._gateway_tools.provision(arguments)
                elif name == "gateway.provision_status":
                    result = await self._gateway_tools.provision_status(arguments)
                elif name == "gateway.list_pending":
                    result = await self._gateway_tools.list_pending(arguments)
                elif name == "gateway.cancel":
                    result = await self._gateway_tools.cancel(arguments)
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

        # Resource handlers - proxy from downstream servers
        @self._server.list_resources()
        async def list_resources() -> list[Resource]:
            resources = self._client_manager.get_all_resources()
            # Filter by policy
            allowed_resources = [
                r
                for r in resources
                if self._policy_manager.is_resource_allowed(r.resource_id)
            ]
            return [
                Resource(
                    uri=AnyUrl(r.uri),
                    name=r.name or r.uri,
                    description=r.description,
                    mimeType=r.mime_type,
                )
                for r in allowed_resources
            ]

        @self._server.read_resource()
        async def read_resource(uri: AnyUrl) -> list[TextResourceContents]:
            # Find resource by URI
            uri_str = str(uri)
            resources = self._client_manager.get_all_resources()
            resource_info = next((r for r in resources if r.uri == uri_str), None)

            if not resource_info:
                raise ValueError(f"Unknown resource: {uri_str}")

            # Check policy
            if not self._policy_manager.is_resource_allowed(resource_info.resource_id):
                raise ValueError(f"Resource blocked by policy: {uri_str}")

            result = await self._client_manager.read_resource(resource_info.resource_id)
            contents = result.get("contents", [])

            # Convert to TextResourceContents
            return [
                TextResourceContents(
                    uri=AnyUrl(c.get("uri", uri_str)),
                    mimeType=c.get("mimeType"),
                    text=c.get("text", ""),
                )
                for c in contents
                if "text" in c  # Only text contents for now
            ]

        # Prompt handlers - proxy from downstream servers
        @self._server.list_prompts()
        async def list_prompts() -> list[Prompt]:
            prompts = self._client_manager.get_all_prompts()
            # Filter by policy
            allowed_prompts = [
                p
                for p in prompts
                if self._policy_manager.is_prompt_allowed(p.prompt_id)
            ]
            return [
                Prompt(
                    name=p.prompt_id,  # Use full ID for uniqueness
                    description=p.description,
                    arguments=[
                        PromptArgument(
                            name=arg.name,
                            description=arg.description,
                            required=arg.required,
                        )
                        for arg in (p.arguments or [])
                    ]
                    if p.arguments
                    else None,
                )
                for p in allowed_prompts
            ]

        @self._server.get_prompt()
        async def get_prompt(
            name: str, arguments: dict[str, str] | None = None
        ) -> GetPromptResult:
            # name is the prompt_id (server::name format)
            # Check policy
            if not self._policy_manager.is_prompt_allowed(name):
                raise ValueError(f"Prompt blocked by policy: {name}")

            result = await self._client_manager.get_prompt(name, arguments)

            # Convert result to GetPromptResult
            messages = result.get("messages", [])
            return GetPromptResult(
                description=result.get("description"),
                messages=[
                    PromptMessage(
                        role=m.get("role", "user"),
                        content=TextContent(
                            type="text", text=m.get("content", {}).get("text", "")
                        ),
                    )
                    for m in messages
                ],
            )

    async def initialize(self) -> None:
        """Initialize connections to downstream servers and generate capability summary."""
        logger.info("Initializing MCP Gateway...")

        # Load pre-built descriptions cache
        cache_path = get_cache_path(self._cache_dir)
        self._descriptions_cache = load_descriptions_cache(cache_path)

        if self._descriptions_cache:
            logger.info(
                f"Loaded pre-built descriptions for {len(self._descriptions_cache.servers)} servers"
            )
        else:
            logger.info("No pre-built descriptions cache found")

        # Load configs from .mcp.json files
        configs = load_configs(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )

        # Filter out the gateway itself to prevent recursive connection
        # Uses command-based detection, not just name matching
        configs = filter_self_references(configs)
        seen_servers = {c.name for c in configs}

        # Load manifest and add auto-start servers (if not already configured)
        manifest = None
        try:
            manifest = load_manifest()
            auto_start_servers = manifest.get_auto_start_servers()

            for server in auto_start_servers:
                if server.name in seen_servers:
                    logger.debug(
                        f"Skipping manifest server '{server.name}' - already in .mcp.json"
                    )
                    continue

                # Skip servers that require API keys if not set
                if server.requires_api_key and server.env_var:
                    if not os.environ.get(server.env_var):
                        logger.info(
                            f"Skipping auto-start server '{server.name}' - "
                            f"missing {server.env_var} (set in .env)"
                        )
                        continue

                # Add manifest server to configs
                configs.append(manifest_server_to_config(server))
                seen_servers.add(server.name)
                logger.info(f"Added auto-start server from manifest: {server.name}")

        except Exception as e:
            logger.warning(f"Failed to load manifest auto-start servers: {e}")

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

        # Start health monitor for heartbeat tracking
        self._client_manager.start_health_monitor()

        statuses = self._client_manager.get_all_server_statuses()
        online = sum(1 for s in statuses if s.status.value == "online")
        tools = self._client_manager.get_all_tools()

        logger.info(
            f"Gateway initialized: {online}/{len(statuses)} servers online, {len(tools)} tools indexed"
        )

        # Generate capability summary for MCP instructions
        # Try pre-built cache first, then LLM, then template
        logger.info("Generating capability summary...")
        self._capability_summary = await generate_capability_summary(
            tools, cache=self._descriptions_cache
        )

        # If no cache and we have tools, auto-generate cache for next time
        if not self._descriptions_cache and tools and manifest:
            logger.info("Auto-generating descriptions cache for future startups...")
            try:
                # Only cache for connected servers (auto_start ones)
                connected_names = [
                    s.name for s in statuses if s.status.value == "online"
                ]
                self._descriptions_cache = await refresh_all(
                    manifest=manifest,
                    cache_path=cache_path,
                    servers=connected_names,
                )
                logger.info(
                    f"Cached descriptions for {len(self._descriptions_cache.servers)} servers"
                )
            except Exception as e:
                logger.warning(f"Failed to auto-generate cache: {e}")

        logger.debug("Capability summary:\n%s", self._capability_summary)

        # Create MCP server with capability instructions
        self._create_server(instructions=self._capability_summary)

    async def run(self) -> None:
        """Run the MCP server (stdio transport)."""
        from mcp.server.stdio import stdio_server

        # Acquire singleton lock to prevent multiple gateway instances
        if not acquire_singleton_lock(self._cache_dir):
            logger.error(
                "Another gateway instance is already running. "
                "Only one gateway should run at a time to prevent recursive spawning."
            )
            raise RuntimeError("Another gateway instance is already running")

        await self.initialize()

        if self._server is None:
            raise RuntimeError("Server not initialized after initialization")

        try:
            async with stdio_server() as (read_stream, write_stream):
                logger.info("MCP Gateway server started")
                await self._server.run(
                    read_stream,
                    write_stream,
                    self._server.create_initialization_options(),
                )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Shutdown the server."""
        logger.info("Shutting down MCP Gateway...")
        try:
            await asyncio.wait_for(self._client_manager.disconnect_all(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out, forcing disconnect")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        finally:
            # Always release singleton lock
            release_singleton_lock()
        logger.info("MCP Gateway shut down")

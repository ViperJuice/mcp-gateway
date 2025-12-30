"""Gateway Tool Implementations."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import load_dotenv
from mcp.types import Tool

from pmcp.client.manager import ClientManager
from pmcp.config.loader import load_configs, manifest_server_to_config
from pmcp.errors import ErrorCode, GatewayException, make_error
from pmcp.identity import filter_self_references
from pmcp.manifest.environment import detect_platform, probe_clis
from pmcp.manifest.installer import (
    MissingApiKeyError,
    get_job_manager,
    InstallError,
)
from pmcp.manifest.loader import load_manifest
from pmcp.manifest.matcher import match_capability
from pmcp.policy.policy import PolicyManager
from pmcp.types import (
    ArgInfo,
    CancelInput,
    CancelOutput,
    CapabilityCandidate,
    CapabilityCard,
    CapabilityRequestInput,
    CapabilityResolution,
    CatalogSearchInput,
    CatalogSearchOutput,
    DescribeInput,
    HealthOutput,
    InvokeInput,
    InvokeOutput,
    InvokeTemplate,
    ListPendingInput,
    ListPendingOutput,
    PendingRequestInfo,
    ProvisionInput,
    ProvisionJobStatus,
    ProvisionOutput,
    ProvisionStatusInput,
    RefreshInput,
    RefreshOutput,
    SchemaCard,
    ServerHealthInfo,
    SyncEnvironmentInput,
    SyncEnvironmentOutput,
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
        Tool(
            name="gateway.request_capability",
            description=(
                "Request a capability by describing what you need in natural language. "
                "The gateway will match your request against installed CLIs and available MCP servers. "
                "If a matching MCP server exists but isn't running, it will be provisioned automatically. "
                "Prefers CLIs over MCP servers when the CLI can fully handle the request."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of the capability needed (e.g., 'I need to scrape a website', 'browser automation')",
                    },
                    "available_clis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: CLIs known to be available in the environment",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="gateway.sync_environment",
            description=(
                "Sync environment information from the host. "
                "Detects the platform (mac/wsl/linux/windows) and probes for installed CLIs. "
                "This information is used to prefer CLIs over MCP servers when matching capabilities."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["mac", "wsl", "linux", "windows"],
                        "description": "Override detected platform (optional)",
                    },
                    "detected_clis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Override detected CLIs (optional)",
                    },
                },
            },
        ),
        Tool(
            name="gateway.provision",
            description=(
                "Provision (install and start) a specific MCP server from the manifest. "
                "Use this after reviewing candidates from gateway.request_capability. "
                "Returns immediately with a job_id for tracking. "
                "Poll gateway.provision_status to check progress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the server to provision (from manifest)",
                    },
                },
                "required": ["server_name"],
            },
        ),
        Tool(
            name="gateway.provision_status",
            description=(
                "Check the status of a running server installation. "
                "Use after gateway.provision returns a job_id. "
                "Returns progress percentage, output log, and final tools when complete."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job ID from gateway.provision response",
                    },
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="gateway.list_pending",
            description=(
                "List all pending tool invocations with health status. "
                "Shows elapsed time, heartbeat age, and current state for each request. "
                "Use this to monitor long-running operations before deciding to cancel."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Filter to pending requests on a specific server (optional)",
                    },
                },
            },
        ),
        Tool(
            name="gateway.cancel",
            description=(
                "Cancel a pending tool invocation. "
                "By default, refuses to cancel healthy requests (recent heartbeat). "
                "Use force=true to cancel anyway. "
                "Use gateway.list_pending first to see request IDs and health status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": 'Request ID in format "server_name::local_id" from gateway.list_pending',
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Force cancel even if request is healthy (has recent heartbeat)",
                    },
                },
                "required": ["request_id"],
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
        self._detected_clis: set[str] | None = None
        self._platform: str | None = None

    async def catalog_search(self, input_data: dict[str, Any]) -> CatalogSearchOutput:
        """gateway.catalog_search - Search for available tools."""
        parsed = CatalogSearchInput.model_validate(input_data)

        tools = self._client_manager.get_all_tools()
        total_available = len(tools)

        # Filter by policy
        tools = [t for t in tools if self._policy_manager.is_tool_allowed(t.tool_id)]

        # Filter by server online status
        if not parsed.include_offline:
            tools = [
                t for t in tools if self._client_manager.is_server_online(t.server_name)
            ]

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
            tools = [
                t for t in tools if RISK_ORDER.get(t.risk_hint.value, 4) <= max_risk
            ]

        # Text search (if query provided) - word-based matching
        if parsed.query:
            query_words = parsed.query.lower().split()
            tools = [
                t
                for t in tools
                if any(
                    word in t.tool_name.lower()
                    or word in t.short_description.lower()
                    or any(word in tag for tag in t.tags)
                    for word in query_words
                )
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
                availability="online"
                if self._client_manager.is_server_online(t.server_name)
                else "offline",
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
            raise GatewayException(
                ErrorCode.E301_TOOL_NOT_FOUND,
                details={"tool_id": parsed.tool_id},
            )

        if not self._policy_manager.is_tool_allowed(parsed.tool_id):
            raise GatewayException(
                ErrorCode.E402_TOOL_DENIED,
                details={"tool_id": parsed.tool_id},
            )

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

        # Build invoke template for direct invocation
        arg_placeholders: dict[str, str] = {}
        for arg in args:
            if arg.required:
                arg_placeholders[arg.name] = f"<required: {arg.type}>"
            else:
                arg_placeholders[arg.name] = f"<optional: {arg.type}>"

        invoke_template = InvokeTemplate(
            tool_id=tool_info.tool_id,
            arguments=arg_placeholders,
        )

        return SchemaCard(
            server=tool_info.server_name,
            tool_name=tool_info.tool_name,
            description=tool_info.description,
            args=args,
            safety_notes=safety_notes if safety_notes else None,
            invoke_template=invoke_template,
        )

    async def invoke(self, input_data: dict[str, Any]) -> InvokeOutput:
        """gateway.invoke - Call a downstream tool."""
        parsed = InvokeInput.model_validate(input_data)

        tool_info = self._client_manager.get_tool(parsed.tool_id)
        if not tool_info:
            error = make_error(
                ErrorCode.E301_TOOL_NOT_FOUND,
                tool_id=parsed.tool_id,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
            )

        if not self._policy_manager.is_tool_allowed(parsed.tool_id):
            error = make_error(
                ErrorCode.E402_TOOL_DENIED,
                tool_id=parsed.tool_id,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
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

        except TimeoutError:
            error = make_error(
                ErrorCode.E303_TOOL_TIMEOUT,
                tool_id=parsed.tool_id,
                timeout_ms=timeout_ms,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
            )

        except ConnectionError as e:
            error = make_error(
                ErrorCode.E201_SERVER_OFFLINE,
                message=str(e),
                tool_id=parsed.tool_id,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
            )

        except Exception as e:
            error = make_error(
                ErrorCode.E302_TOOL_EXECUTION_FAILED,
                message=str(e),
                tool_id=parsed.tool_id,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
            )

    async def refresh(self, input_data: dict[str, Any]) -> RefreshOutput:
        """gateway.refresh - Reload backend configs and reconnect."""
        parsed = RefreshInput.model_validate(input_data)

        logger.info(f"Refresh requested: {parsed.reason or 'manual refresh'}")

        try:
            # Reload configs from .mcp.json files
            configs = load_configs(
                project_root=self._project_root,
                custom_config_path=self._custom_config_path,
            )

            # Filter out the gateway itself to prevent recursive connection
            # Uses command-based detection, not just name matching
            configs = filter_self_references(configs)
            seen_servers = {c.name for c in configs}

            # Load manifest and add auto-start servers (if not already configured)
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
                                f"missing {server.env_var}"
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

    def _check_api_key_available(self, env_var: str | None) -> bool:
        """Check if an API key is available in environment or .env file."""
        if not env_var:
            return False

        # Check environment first
        if os.environ.get(env_var):
            return True

        # Check .env file
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            return bool(os.environ.get(env_var))

        return False

    async def request_capability(
        self, input_data: dict[str, Any]
    ) -> CapabilityResolution:
        """gateway.request_capability - Request a capability by natural language.

        Returns ranked candidates for Claude Code to choose from.
        Use gateway.provision to actually install/start the chosen server.
        """
        parsed = CapabilityRequestInput.model_validate(input_data)

        # Load manifest
        manifest = load_manifest()

        # Get detected CLIs (from input or probe)
        if parsed.available_clis:
            detected_clis = list(parsed.available_clis)
        elif self._detected_clis is not None:
            detected_clis = list(self._detected_clis)
        else:
            # Probe environment if not yet done
            platform = detect_platform()
            cli_configs = {
                name: {
                    "check_command": cli.check_command,
                    "help_command": cli.help_command,
                }
                for name, cli in manifest.cli_alternatives.items()
            }
            detected_cli_infos = await probe_clis(cli_configs)
            detected_clis = list(detected_cli_infos.keys())
            self._detected_clis = set(detected_clis)
            self._platform = platform

        # Get running servers
        running_servers = [
            s.name
            for s in self._client_manager.get_all_server_statuses()
            if s.status.value == "online"
        ]

        # Try BAML matching
        try:
            from pmcp.baml_client import b
            from pmcp.baml_client.types import (
                ManifestCLI,
                ManifestServer,
                ManifestSummary,
            )

            # Build manifest summary for BAML
            servers = [
                ManifestServer(
                    name=name,
                    description=server.description,
                    keywords=server.keywords,
                    requires_api_key=server.requires_api_key,
                    env_var=server.env_var,
                )
                for name, server in manifest.servers.items()
            ]
            clis = [
                ManifestCLI(
                    name=name,
                    description=cli.description,
                    keywords=cli.keywords,
                )
                for name, cli in manifest.cli_alternatives.items()
            ]
            manifest_summary = ManifestSummary(servers=servers, clis=clis)

            # Call BAML
            import asyncio

            async with asyncio.timeout(15):
                result = await b.MatchCapability(
                    query=parsed.query,
                    manifest=manifest_summary,
                    available_clis=detected_clis,
                    running_servers=running_servers,
                )

            # Convert BAML result to our types with enriched info
            candidates: list[CapabilityCandidate] = []
            for c in result.candidates:
                # Enrich with runtime info
                is_installed = (
                    c.name in detected_clis if c.candidate_type == "cli" else False
                )
                is_running = (
                    c.name in running_servers if c.candidate_type == "server" else False
                )

                # Get server info for API key details
                env_var = None
                env_instructions = None
                api_key_available = False
                requires_api_key = c.requires_api_key

                if c.candidate_type == "server":
                    server_config = manifest.get_server(c.name)
                    if server_config:
                        env_var = server_config.env_var
                        env_instructions = server_config.env_instructions
                        api_key_available = self._check_api_key_available(env_var)

                candidates.append(
                    CapabilityCandidate(
                        name=c.name,
                        candidate_type=cast(Literal["cli", "server"], c.candidate_type),
                        relevance_score=c.relevance_score,
                        reasoning=c.reasoning,
                        requires_api_key=requires_api_key,
                        api_key_available=api_key_available,
                        env_var=env_var,
                        env_instructions=env_instructions,
                        is_installed=is_installed,
                        is_running=is_running,
                    )
                )

            if not candidates:
                logger.info(f"No matches for capability request: {parsed.query}")
                return CapabilityResolution(
                    status="not_available",
                    message=f"No matching capability found for: {parsed.query}",
                    logged_for_discovery=True,
                )

            # Return candidates for Claude to choose
            return CapabilityResolution(
                status="candidates",
                message=f"Found {len(candidates)} matching options. Review and call gateway.provision to install your choice.",
                candidates=candidates,
                recommendation=result.recommendation,
            )

        except ImportError:
            logger.warning("BAML not available, falling back to keyword matching")
        except Exception as e:
            logger.warning(
                f"BAML matching failed: {e}, falling back to keyword matching"
            )

        # Fallback: use simple keyword matching
        match_result = await match_capability(
            query=parsed.query,
            manifest=manifest,
            detected_clis=set(detected_clis),
            use_llm=False,  # Don't use LLM in fallback
        )

        if not match_result.matched:
            logger.info(f"Unmatched capability request: {parsed.query}")
            return CapabilityResolution(
                status="not_available",
                message=f"No matching capability found for: {parsed.query}",
                logged_for_discovery=True,
            )

        # Build single candidate from keyword match
        if match_result.entry_type == "cli":
            candidates = [
                CapabilityCandidate(
                    name=match_result.entry_name,
                    candidate_type="cli",
                    relevance_score=match_result.confidence,
                    reasoning=match_result.reasoning,
                    is_installed=match_result.entry_name in detected_clis,
                )
            ]
        else:
            server_config = manifest.get_server(match_result.entry_name)
            env_var = server_config.env_var if server_config else None
            candidates = [
                CapabilityCandidate(
                    name=match_result.entry_name,
                    candidate_type="server",
                    relevance_score=match_result.confidence,
                    reasoning=match_result.reasoning,
                    requires_api_key=server_config.requires_api_key
                    if server_config
                    else False,
                    api_key_available=self._check_api_key_available(env_var),
                    env_var=env_var,
                    env_instructions=server_config.env_instructions
                    if server_config
                    else None,
                    is_running=match_result.entry_name in running_servers,
                )
            ]

        return CapabilityResolution(
            status="candidates",
            message=f"Found {len(candidates)} matching option. Review and call gateway.provision to install.",
            candidates=candidates,
            recommendation=f"Use {match_result.entry_name} ({match_result.entry_type})",
        )

    async def provision(self, input_data: dict[str, Any]) -> ProvisionOutput:
        """gateway.provision - Start background installation of an MCP server."""
        parsed = ProvisionInput.model_validate(input_data)
        server_name = parsed.server_name

        # Load manifest
        manifest = load_manifest()
        server_config = manifest.get_server(server_name)

        if not server_config:
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=f"Server '{server_name}' not found in manifest.",
            )

        # Check if already running
        if self._client_manager.is_server_online(server_name):
            tools = [
                t
                for t in self._client_manager.get_all_tools()
                if t.server_name == server_name
            ]
            return ProvisionOutput(
                ok=True,
                server=server_name,
                status="already_running",
                message=f"Server '{server_name}' is already running with {len(tools)} tools.",
                new_tools=[
                    CapabilityCard(
                        tool_id=t.tool_id,
                        server=t.server_name,
                        tool_name=t.tool_name,
                        short_description=t.short_description,
                        tags=t.tags,
                        availability="online",
                        risk_hint=t.risk_hint.value,
                    )
                    for t in tools[:10]
                ],
            )

        # Check API key if required
        if server_config.requires_api_key and server_config.env_var:
            if not self._check_api_key_available(server_config.env_var):
                return ProvisionOutput(
                    ok=False,
                    server=server_name,
                    status="failed",
                    message=f"Server '{server_name}' requires API key {server_config.env_var}.",
                    needs_api_key=True,
                    env_var=server_config.env_var,
                    env_instructions=server_config.env_instructions,
                )

        # Start background installation
        platform = getattr(self, "_platform", None) or detect_platform()
        job_manager = get_job_manager()

        try:
            job_id = await job_manager.start_install(server_config, platform)

            return ProvisionOutput(
                ok=True,
                server=server_name,
                status="started",
                job_id=job_id,
                message=f"Installation started for '{server_name}'. Poll gateway.provision_status('{job_id}') for progress.",
            )

        except MissingApiKeyError as e:
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=f"Server '{server_name}' requires API key.",
                needs_api_key=True,
                env_var=e.env_var,
                env_instructions=e.env_instructions,
            )

        except InstallError as e:
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=str(e),
            )

        except Exception as e:
            logger.error(f"Failed to start provisioning {server_name}: {e}")
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=f"Failed to start provisioning '{server_name}': {e}",
            )

    async def provision_status(self, input_data: dict[str, Any]) -> ProvisionJobStatus:
        """gateway.provision_status - Check status of a running installation."""
        import time

        try:
            parsed = ProvisionStatusInput.model_validate(input_data)
            job_id = parsed.job_id

            job_manager = get_job_manager()
            job = job_manager.get_job(job_id)

            if not job:
                return ProvisionJobStatus(
                    job_id=job_id,
                    server="unknown",
                    status="not_found",
                    progress=0,
                    message=f"Job '{job_id}' not found. It may have expired.",
                )

            # Copy job state to avoid race conditions with monitor task
            job_status = job.status
            job_progress = job.progress
            job_error = job.error
            job_server_name = job.server_name
            job_output_lines = list(job.output_lines)  # Copy the list
            elapsed = time.time() - job.started_at

            logger.debug(
                f"provision_status: job={job_id} status={job_status} progress={job_progress}"
            )

            # If server_ready, perform handoff to ClientManager
            if job_status == "server_ready":
                process = job.process
                if not process or process.returncode is not None:
                    # Process died before handoff
                    job.status = "failed"
                    job.error = "Server process exited before handoff"
                    return ProvisionJobStatus(
                        job_id=job_id,
                        server=job_server_name,
                        status="failed",
                        progress=job_progress,
                        message=f"Server process for '{job_server_name}' exited unexpectedly",
                        output_tail=job_output_lines[-5:],
                        elapsed_seconds=elapsed,
                        error="Process exited before handoff",
                    )

                try:
                    # Build config from manifest
                    manifest = load_manifest()
                    server_config = manifest.get_server(job_server_name)
                    if not server_config:
                        raise ValueError(
                            f"Server '{job_server_name}' not found in manifest"
                        )

                    resolved_config = manifest_server_to_config(server_config)

                    # Adopt the process into ClientManager
                    await self._client_manager.adopt_process(
                        job_server_name, process, resolved_config
                    )

                    # Mark job complete and clear process reference
                    job.status = "complete"
                    job.process = None

                    # Get the new tools
                    tools = [
                        t
                        for t in self._client_manager.get_all_tools()
                        if t.server_name == job_server_name
                    ]

                    return ProvisionJobStatus(
                        job_id=job_id,
                        server=job_server_name,
                        status="complete",
                        progress=100,
                        message=f"Server '{job_server_name}' installed and connected with {len(tools)} tools.",
                        output_tail=job_output_lines[-5:],
                        elapsed_seconds=elapsed,
                        new_tools=[
                            CapabilityCard(
                                tool_id=t.tool_id,
                                server=t.server_name,
                                tool_name=t.tool_name,
                                short_description=t.short_description,
                                tags=t.tags,
                                availability="online",
                                risk_hint=t.risk_hint.value,
                            )
                            for t in tools[:10]
                        ]
                        if tools
                        else None,
                    )

                except Exception as e:
                    logger.error(
                        f"Handoff failed for {job_server_name}: {e}", exc_info=True
                    )
                    job.status = "failed"
                    job.error = f"Handoff failed: {e}"
                    # Kill the orphaned process
                    if process and process.returncode is None:
                        try:
                            process.kill()
                        except Exception:
                            pass
                    job.process = None

                    return ProvisionJobStatus(
                        job_id=job_id,
                        server=job_server_name,
                        status="failed",
                        progress=job_progress,
                        message=f"Failed to connect to '{job_server_name}': {e}",
                        output_tail=job_output_lines[-5:],
                        elapsed_seconds=elapsed,
                        error=str(e),
                    )

            # If complete (from non-npx install), trigger refresh to connect the server
            if job_status == "complete":
                tools = []
                refresh_error = None

                try:
                    await self.refresh({"reason": f"Provisioned {job_server_name}"})
                    # Get the new tools
                    tools = [
                        t
                        for t in self._client_manager.get_all_tools()
                        if t.server_name == job_server_name
                    ]
                except Exception as e:
                    logger.error(f"Failed to refresh after install: {e}")
                    refresh_error = str(e)

                message = f"Server '{job_server_name}' installed"
                if tools:
                    message += f" and connected with {len(tools)} tools."
                elif refresh_error:
                    message += f" but refresh failed: {refresh_error}"
                else:
                    message += " but no tools found. Try gateway.refresh manually."

                return ProvisionJobStatus(
                    job_id=job_id,
                    server=job_server_name,
                    status="complete",
                    progress=100,
                    message=message,
                    output_tail=job_output_lines[-5:],
                    elapsed_seconds=elapsed,
                    new_tools=[
                        CapabilityCard(
                            tool_id=t.tool_id,
                            server=t.server_name,
                            tool_name=t.tool_name,
                            short_description=t.short_description,
                            tags=t.tags,
                            availability="online",
                            risk_hint=t.risk_hint.value,
                        )
                        for t in tools[:10]
                    ]
                    if tools
                    else None,
                    error=refresh_error,
                )

            # For other statuses, return current state
            status_messages = {
                "pending": f"Preparing to install '{job_server_name}'...",
                "installing": f"Installing '{job_server_name}'... ({job_progress}%)",
                "server_ready": f"Server '{job_server_name}' starting, connecting...",
                "failed": f"Installation failed: {job_error}",
                "timeout": f"Installation timed out: {job_error}",
            }

            return ProvisionJobStatus(
                job_id=job_id,
                server=job_server_name,
                status=job_status,
                progress=job_progress,
                message=status_messages.get(job_status, f"Status: {job_status}"),
                output_tail=job_output_lines[-5:],
                elapsed_seconds=elapsed,
                error=job_error,
            )

        except Exception as e:
            logger.error(f"provision_status handler failed: {e}", exc_info=True)
            # Return a safe error response instead of crashing
            return ProvisionJobStatus(
                job_id=input_data.get("job_id", "unknown"),
                server="unknown",
                status="failed",
                progress=0,
                message=f"Error checking status: {e}",
                error=str(e),
            )

    async def sync_environment(
        self, input_data: dict[str, Any]
    ) -> SyncEnvironmentOutput:
        """gateway.sync_environment - Sync environment info from host."""
        parsed = SyncEnvironmentInput.model_validate(input_data)

        # Use provided or detect platform
        if parsed.platform:
            platform = parsed.platform
        else:
            platform = detect_platform()

        # Use provided or probe CLIs
        if parsed.detected_clis:
            detected_clis = set(parsed.detected_clis)
        else:
            manifest = load_manifest()
            # Build CLI configs dict for probing
            cli_configs = {
                name: {
                    "check_command": cli.check_command,
                    "help_command": cli.help_command,
                }
                for name, cli in manifest.cli_alternatives.items()
            }
            detected_cli_infos = await probe_clis(cli_configs)
            detected_clis = set(detected_cli_infos.keys())

        # Store for future use
        self._platform = platform
        self._detected_clis = detected_clis

        return SyncEnvironmentOutput(
            platform=platform,
            detected_clis=list(detected_clis),
            message=f"Environment synced: {platform} with {len(detected_clis)} CLIs detected.",
        )

    async def list_pending(self, input_data: dict[str, Any]) -> ListPendingOutput:
        """gateway.list_pending - List pending tool invocations with health status."""
        import time
        from datetime import datetime, timezone

        parsed = ListPendingInput.model_validate(input_data)

        pending_requests = self._client_manager.get_pending_requests(parsed.server)
        now = time.time()

        requests: list[PendingRequestInfo] = []
        for req in pending_requests:
            state = self._client_manager.get_request_state(req)
            requests.append(
                PendingRequestInfo(
                    request_id=f"{req.server_name}::{req.request_id}",
                    server_name=req.server_name,
                    tool_id=req.tool_id,
                    started_at_iso=datetime.fromtimestamp(
                        req.started_at, tz=timezone.utc
                    ).isoformat(),
                    elapsed_seconds=now - req.started_at,
                    timeout_ms=req.timeout_ms,
                    state=state.value,
                    last_heartbeat_seconds_ago=now - req.last_heartbeat,
                )
            )

        return ListPendingOutput(
            requests=requests,
            total_pending=len(requests),
        )

    async def cancel(self, input_data: dict[str, Any]) -> CancelOutput:
        """gateway.cancel - Cancel a pending tool invocation."""
        parsed = CancelInput.model_validate(input_data)

        (
            status,
            message,
            was_stalled,
            elapsed,
        ) = await self._client_manager.cancel_request(parsed.request_id, parsed.force)

        return CancelOutput(
            request_id=parsed.request_id,
            status=status,
            message=message,
            was_stalled=was_stalled,
            elapsed_seconds=elapsed,
        )

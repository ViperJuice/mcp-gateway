"""MCP Client Manager - Manages connections to downstream MCP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any

from mcp_gateway.config.loader import make_tool_id
from mcp_gateway.types import (
    ResolvedServerConfig,
    RiskHint,
    ServerStatus,
    ServerStatusEnum,
    ToolInfo,
)

logger = logging.getLogger(__name__)


def _generate_revision_id() -> str:
    """Generate a revision ID for cache invalidation."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"rev-{int(time.time() * 1000)}-{suffix}"


def _infer_risk_hint(tool_name: str, description: str) -> RiskHint:
    """Infer risk level from tool name/description."""
    low_risk_patterns = ["read", "get", "list", "search", "query", "fetch", "describe"]
    high_risk_patterns = [
        "delete",
        "remove",
        "drop",
        "execute",
        "run",
        "write",
        "create",
        "update",
        "modify",
        "send",
        "post",
        "put",
    ]

    combined = f"{tool_name} {description}".lower()

    for pattern in high_risk_patterns:
        if pattern in combined:
            return RiskHint.HIGH

    for pattern in low_risk_patterns:
        if pattern in combined:
            return RiskHint.LOW

    return RiskHint.MEDIUM


def _extract_tags(server_name: str, tool_name: str, description: str) -> list[str]:
    """Extract tags from tool name/description."""
    tags: set[str] = {server_name}

    categories: dict[str, list[str]] = {
        "database": ["db", "sql", "query", "table", "database"],
        "file": ["file", "directory", "folder", "path"],
        "git": ["git", "commit", "branch", "repository", "repo"],
        "http": ["http", "api", "request", "fetch", "url"],
        "search": ["search", "find", "grep", "filter"],
        "code": ["code", "function", "class", "symbol"],
    }

    combined = f"{tool_name} {description}".lower()

    for category, keywords in categories.items():
        for keyword in keywords:
            if keyword in combined:
                tags.add(category)
                break

    return list(tags)


def _truncate_description(description: str, max_length: int = 100) -> str:
    """Truncate description for catalog display."""
    if not description:
        return ""
    if len(description) <= max_length:
        return description
    return description[: max_length - 3] + "..."


@dataclass
class ManagedClient:
    """A managed connection to a downstream MCP server."""

    config: ResolvedServerConfig
    process: asyncio.subprocess.Process | None = None
    status: ServerStatus = field(default_factory=lambda: ServerStatus(
        name="",
        status=ServerStatusEnum.OFFLINE,
        tool_count=0,
    ))
    request_id: int = 0
    pending_requests: dict[int, asyncio.Future[Any]] = field(default_factory=dict)
    read_task: asyncio.Task[None] | None = None


class ClientManager:
    """Manages connections to downstream MCP servers."""

    def __init__(self, max_tools_per_server: int = 100) -> None:
        self._clients: dict[str, ManagedClient] = {}
        self._tools: dict[str, ToolInfo] = {}
        self._servers: dict[str, ServerStatus] = {}
        self._revision_id: str = _generate_revision_id()
        self._last_refresh_ts: float = time.time()
        self._max_tools_per_server = max_tools_per_server

    async def connect_all(self, configs: list[ResolvedServerConfig]) -> list[str]:
        """Connect to all configured servers."""
        errors: list[str] = []

        for config in configs:
            try:
                await self._connect_server(config)
            except Exception as e:
                error_msg = f"Failed to connect to {config.name}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        self._revision_id = _generate_revision_id()
        self._last_refresh_ts = time.time()

        return errors

    async def _connect_server(self, config: ResolvedServerConfig) -> None:
        """Connect to a single MCP server."""
        name = config.name

        # Initialize status
        status = ServerStatus(
            name=name,
            status=ServerStatusEnum.CONNECTING,
            tool_count=0,
        )
        self._servers[name] = status

        if not config.config.command:
            raise ValueError(f"Server {name} missing command - only stdio transport supported")

        logger.info(f"Connecting to MCP server: {name}")

        # Build environment
        env = os.environ.copy()
        if config.config.env:
            env.update(config.config.env)

        # Spawn process
        process = await asyncio.create_subprocess_exec(
            config.config.command,
            *config.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.config.cwd,
            env=env,
        )

        managed = ManagedClient(
            config=config,
            process=process,
            status=status,
        )
        self._clients[name] = managed

        # Start reading stderr in background
        if process.stderr:
            asyncio.create_task(self._read_stderr(name, process.stderr))

        try:
            # Start reading stdout
            managed.read_task = asyncio.create_task(self._read_stdout(name, managed))

            # Initialize connection
            await self._send_initialize(managed)

            # List tools
            tools_result = await self._send_request(managed, "tools/list", {})
            tools = tools_result.get("tools", [])

            # Index tools
            indexed = 0
            for tool in tools:
                if indexed >= self._max_tools_per_server:
                    logger.warning(
                        f"Server {name} has more than {self._max_tools_per_server} tools, truncating"
                    )
                    break

                tool_id = make_tool_id(name, tool["name"])
                description = tool.get("description", "")

                tool_info = ToolInfo(
                    tool_id=tool_id,
                    server_name=name,
                    tool_name=tool["name"],
                    description=description,
                    short_description=_truncate_description(description),
                    input_schema=tool.get("inputSchema", {}),
                    tags=_extract_tags(name, tool["name"], description),
                    risk_hint=_infer_risk_hint(tool["name"], description),
                )

                self._tools[tool_id] = tool_info
                indexed += 1

            # Update status
            status.status = ServerStatusEnum.ONLINE
            status.tool_count = indexed
            status.last_connected_at = time.time()

            logger.info(f"Connected to {name}: {indexed} tools indexed")

        except Exception as e:
            status.status = ServerStatusEnum.ERROR
            status.last_error = str(e)
            if process.returncode is None:
                process.kill()
            raise

    async def _read_stderr(
        self, name: str, stderr: asyncio.StreamReader
    ) -> None:
        """Read stderr from a server process."""
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                logger.debug(f"[{name}] stderr: {line.decode().strip()}")
        except Exception:
            pass

    async def _read_stdout(self, name: str, managed: ManagedClient) -> None:
        """Read JSON-RPC messages from stdout."""
        if not managed.process or not managed.process.stdout:
            return

        try:
            while True:
                line = await managed.process.stdout.readline()
                if not line:
                    break

                try:
                    message = json.loads(line.decode())
                    msg_id = message.get("id")
                    if msg_id is not None and msg_id in managed.pending_requests:
                        future = managed.pending_requests.pop(msg_id)
                        if "error" in message:
                            future.set_exception(
                                Exception(message["error"].get("message", "Unknown error"))
                            )
                        else:
                            future.set_result(message.get("result", {}))
                except json.JSONDecodeError:
                    logger.debug(f"[{name}] Non-JSON output: {line.decode().strip()}")
        except Exception as e:
            logger.debug(f"[{name}] Read error: {e}")

    async def _send_request(
        self, managed: ManagedClient, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for response."""
        if not managed.process or not managed.process.stdin:
            raise RuntimeError("Process not running")

        managed.request_id += 1
        request_id = managed.request_id

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        # Create future for response
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        managed.pending_requests[request_id] = future

        # Send request
        data = json.dumps(request) + "\n"
        managed.process.stdin.write(data.encode())
        await managed.process.stdin.drain()

        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            return result
        except asyncio.TimeoutError:
            managed.pending_requests.pop(request_id, None)
            raise TimeoutError(f"Request {method} timed out")

    async def _send_initialize(self, managed: ManagedClient) -> None:
        """Send initialize handshake."""
        await self._send_request(
            managed,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"},
            },
        )

        # Send initialized notification (no response expected)
        if managed.process and managed.process.stdin:
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            data = json.dumps(notification) + "\n"
            managed.process.stdin.write(data.encode())
            await managed.process.stdin.drain()

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for name, managed in self._clients.items():
            try:
                logger.info(f"Disconnecting from {name}")
                if managed.read_task:
                    managed.read_task.cancel()
                if managed.process and managed.process.returncode is None:
                    managed.process.terminate()
                    try:
                        await asyncio.wait_for(managed.process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        managed.process.kill()
            except Exception as e:
                logger.warning(f"Error disconnecting from {name}: {e}")

        self._clients.clear()
        self._tools.clear()
        self._servers.clear()

    async def refresh(self, configs: list[ResolvedServerConfig]) -> list[str]:
        """Refresh connections (disconnect + reconnect)."""
        await self.disconnect_all()
        return await self.connect_all(configs)

    async def adopt_process(
        self,
        name: str,
        process: asyncio.subprocess.Process,
        config: ResolvedServerConfig,
    ) -> None:
        """Adopt an already-running subprocess as a managed MCP client.

        Used when npx-based servers start during installation.
        The process must have stdin/stdout pipes available.

        Args:
            name: Server name
            process: Running subprocess with stdin/stdout pipes
            config: Server configuration

        Raises:
            RuntimeError: If process is not running or missing pipes
            Exception: If MCP initialization fails
        """
        # Validate process state
        if process.returncode is not None:
            raise RuntimeError(f"Process for {name} has already exited")
        if not process.stdin:
            raise RuntimeError(f"Process for {name} has no stdin pipe")
        if not process.stdout:
            raise RuntimeError(f"Process for {name} has no stdout pipe")

        logger.info(f"Adopting process for MCP server: {name}")

        # Initialize status
        status = ServerStatus(
            name=name,
            status=ServerStatusEnum.CONNECTING,
            tool_count=0,
        )
        self._servers[name] = status

        managed = ManagedClient(
            config=config,
            process=process,
            status=status,
        )
        self._clients[name] = managed

        # Start reading stderr in background (if available)
        if process.stderr:
            asyncio.create_task(self._read_stderr(name, process.stderr))

        try:
            # Start reading stdout for JSON-RPC responses
            managed.read_task = asyncio.create_task(self._read_stdout(name, managed))

            # Initialize MCP connection
            await self._send_initialize(managed)

            # List tools
            tools_result = await self._send_request(managed, "tools/list", {})
            tools = tools_result.get("tools", [])

            # Index tools
            indexed = 0
            for tool in tools:
                if indexed >= self._max_tools_per_server:
                    logger.warning(
                        f"Server {name} has more than {self._max_tools_per_server} tools, truncating"
                    )
                    break

                tool_id = make_tool_id(name, tool["name"])
                description = tool.get("description", "")

                tool_info = ToolInfo(
                    tool_id=tool_id,
                    server_name=name,
                    tool_name=tool["name"],
                    description=description,
                    short_description=_truncate_description(description),
                    input_schema=tool.get("inputSchema", {}),
                    tags=_extract_tags(name, tool["name"], description),
                    risk_hint=_infer_risk_hint(tool["name"], description),
                )

                self._tools[tool_id] = tool_info
                indexed += 1

            # Update status
            status.status = ServerStatusEnum.ONLINE
            status.tool_count = indexed
            status.last_connected_at = time.time()

            # Update revision
            self._revision_id = _generate_revision_id()
            self._last_refresh_ts = time.time()

            logger.info(f"Adopted {name}: {indexed} tools indexed")

        except Exception as e:
            status.status = ServerStatusEnum.ERROR
            status.last_error = str(e)
            # Clean up on failure
            if managed.read_task:
                managed.read_task.cancel()
            if process.returncode is None:
                process.kill()
            # Remove from registries
            self._clients.pop(name, None)
            self._servers.pop(name, None)
            raise

    async def call_tool(
        self, tool_id: str, args: dict[str, Any], timeout_ms: int = 30000
    ) -> Any:
        """Call a tool on a downstream server."""
        tool_info = self._tools.get(tool_id)
        if not tool_info:
            raise ValueError(f"Unknown tool: {tool_id}")

        managed = self._clients.get(tool_info.server_name)
        if not managed or not managed.process:
            raise RuntimeError(f"Server {tool_info.server_name} is not connected")

        if managed.status.status != ServerStatusEnum.ONLINE:
            raise RuntimeError(f"Server {tool_info.server_name} is {managed.status.status.value}")

        # Send tool call
        result = await asyncio.wait_for(
            self._send_request(
                managed,
                "tools/call",
                {"name": tool_info.tool_name, "arguments": args},
            ),
            timeout=timeout_ms / 1000.0,
        )

        return result

    def get_tool(self, tool_id: str) -> ToolInfo | None:
        """Get tool info by ID."""
        return self._tools.get(tool_id)

    def get_all_tools(self) -> list[ToolInfo]:
        """Get all tools."""
        return list(self._tools.values())

    def get_server_status(self, name: str) -> ServerStatus | None:
        """Get server status."""
        return self._servers.get(name)

    def get_all_server_statuses(self) -> list[ServerStatus]:
        """Get all server statuses."""
        return list(self._servers.values())

    def get_registry_meta(self) -> tuple[str, float]:
        """Get registry metadata (revision_id, last_refresh_ts)."""
        return (self._revision_id, self._last_refresh_ts)

    def is_server_online(self, name: str) -> bool:
        """Check if server is online."""
        status = self._servers.get(name)
        return status is not None and status.status == ServerStatusEnum.ONLINE

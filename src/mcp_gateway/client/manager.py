"""MCP Client Manager - Manages connections to downstream MCP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import string
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from mcp_gateway.config.loader import make_tool_id
from mcp_gateway.types import (
    ResolvedServerConfig,
    RequestState,
    RiskHint,
    ServerStatus,
    ServerStatusEnum,
    ToolInfo,
)

logger = logging.getLogger(__name__)

# Heartbeat thresholds for health monitoring
HEARTBEAT_WARN_THRESHOLD = 60.0  # Warn if no activity for 60s
HEARTBEAT_STALL_THRESHOLD = 120.0  # Mark as stalled after 120s
HEALTH_CHECK_INTERVAL = 30.0  # Background health check every 30s


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
class PendingRequest:
    """Metadata for tracking a pending tool invocation."""

    request_id: int
    server_name: str
    tool_id: str  # Empty for non-tool requests (initialize, tools/list)
    started_at: float  # time.time() when request started
    last_heartbeat: float  # time.time() of last activity
    timeout_ms: int  # Configured timeout
    future: asyncio.Future[Any]


@dataclass
class ManagedClient:
    """A managed connection to a downstream MCP server."""

    config: ResolvedServerConfig
    process: asyncio.subprocess.Process | None = None
    status: ServerStatus = field(
        default_factory=lambda: ServerStatus(
            name="",
            status=ServerStatusEnum.OFFLINE,
            tool_count=0,
        )
    )
    request_id: int = 0
    pending_requests: dict[int, PendingRequest] = field(default_factory=dict)
    read_task: asyncio.Task[None] | None = None
    # Health monitoring: rolling window of response times for avg calculation
    response_times: deque[float] = field(default_factory=lambda: deque(maxlen=100))


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
            raise ValueError(
                f"Server {name} missing command - only stdio transport supported"
            )

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

    async def _read_stderr(self, name: str, stderr: asyncio.StreamReader) -> None:
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
                    # EOF - server process has exited
                    break

                # UPDATE heartbeat on ANY output from server
                now = time.time()
                managed.status.last_activity_at = now

                try:
                    message = json.loads(line.decode())
                    msg_id = message.get("id")
                    if msg_id is not None and msg_id in managed.pending_requests:
                        pending = managed.pending_requests.pop(msg_id)
                        pending.last_heartbeat = now  # Update request heartbeat

                        # Track response time
                        elapsed_ms = (now - pending.started_at) * 1000
                        managed.response_times.append(elapsed_ms)
                        if managed.response_times:
                            managed.status.avg_response_time_ms = sum(
                                managed.response_times
                            ) / len(managed.response_times)

                        # Update pending count
                        managed.status.pending_request_count = len(
                            managed.pending_requests
                        )

                        if "error" in message:
                            pending.future.set_exception(
                                Exception(
                                    message["error"].get("message", "Unknown error")
                                )
                            )
                        else:
                            pending.future.set_result(message.get("result", {}))
                except json.JSONDecodeError:
                    # Non-JSON output still counts as heartbeat for all pending
                    for req in managed.pending_requests.values():
                        req.last_heartbeat = now
                    logger.debug(f"[{name}] Non-JSON output: {line.decode().strip()}")
        except Exception as e:
            logger.debug(f"[{name}] Read error: {e}")
        finally:
            # Mark server as offline when stdout closes
            if managed.status.status == ServerStatusEnum.ONLINE:
                logger.warning(f"Server {name} disconnected unexpectedly")
                managed.status.status = ServerStatusEnum.ERROR
                managed.status.last_error = "Server process exited"
            # Cancel any pending requests
            for request_id, pending in list(managed.pending_requests.items()):
                if not pending.future.done():
                    pending.future.set_exception(
                        ConnectionError(f"Server {name} disconnected")
                    )
            managed.pending_requests.clear()
            managed.status.pending_request_count = 0

    async def _send_request(
        self,
        managed: ManagedClient,
        method: str,
        params: dict[str, Any],
        tool_id: str = "",
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for response."""
        if not managed.process or not managed.process.stdin:
            raise RuntimeError("Process not running")

        managed.request_id += 1
        request_id = managed.request_id
        now = time.time()

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        # Create PendingRequest with metadata for health monitoring
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        pending = PendingRequest(
            request_id=request_id,
            server_name=managed.config.name,
            tool_id=tool_id,
            started_at=now,
            last_heartbeat=now,
            timeout_ms=timeout_ms,
            future=future,
        )
        managed.pending_requests[request_id] = pending
        managed.status.pending_request_count = len(managed.pending_requests)

        # Send request
        data = json.dumps(request) + "\n"
        managed.process.stdin.write(data.encode())
        await managed.process.stdin.drain()

        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=timeout_ms / 1000.0)
            return result
        except asyncio.TimeoutError:
            managed.pending_requests.pop(request_id, None)
            managed.status.pending_request_count = len(managed.pending_requests)
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
        # Stop health monitor if running
        self.stop_health_monitor()

        for name, managed in self._clients.items():
            try:
                logger.info(f"Disconnecting from {name}")

                # Cancel pending requests first
                for request_id, pending in list(managed.pending_requests.items()):
                    if not pending.future.done():
                        pending.future.cancel()
                managed.pending_requests.clear()
                managed.status.pending_request_count = 0

                # Cancel read task
                if managed.read_task:
                    managed.read_task.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(managed.read_task), timeout=1.0
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

                # Terminate process
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
            raise RuntimeError(
                f"Server {tool_info.server_name} is {managed.status.status.value}"
            )

        # Send tool call with metadata for health monitoring
        result = await self._send_request(
            managed,
            "tools/call",
            {"name": tool_info.tool_name, "arguments": args},
            tool_id=tool_id,
            timeout_ms=timeout_ms,
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

    # === Health Monitoring Methods ===

    def start_health_monitor(self) -> None:
        """Start the background health monitoring task."""
        if not hasattr(self, "_health_task") or self._health_task is None:
            self._health_task: asyncio.Task[None] | None = asyncio.create_task(
                self._health_monitor_loop()
            )
            logger.info("Started health monitor background task")

    def stop_health_monitor(self) -> None:
        """Stop the health monitoring task."""
        if hasattr(self, "_health_task") and self._health_task:
            self._health_task.cancel()
            self._health_task = None
            logger.debug("Stopped health monitor background task")

    async def _health_monitor_loop(self) -> None:
        """Background task to monitor server and request health."""
        while True:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                now = time.time()

                for name, managed in self._clients.items():
                    # Check process health
                    if managed.process:
                        returncode = managed.process.returncode
                        if returncode is not None:
                            logger.warning(
                                f"Server {name} process exited with code {returncode}"
                            )
                            managed.status.status = ServerStatusEnum.ERROR
                            managed.status.last_error = f"Process exited: {returncode}"
                            continue

                    # Check for stalled requests
                    for req_id, pending in list(managed.pending_requests.items()):
                        elapsed_since_heartbeat = now - pending.last_heartbeat

                        if elapsed_since_heartbeat > HEARTBEAT_STALL_THRESHOLD:
                            logger.warning(
                                f"Request {name}::{req_id} stalled "
                                f"(no heartbeat for {elapsed_since_heartbeat:.0f}s)"
                            )
                        elif elapsed_since_heartbeat > HEARTBEAT_WARN_THRESHOLD:
                            logger.info(
                                f"Request {name}::{req_id} slow "
                                f"(no heartbeat for {elapsed_since_heartbeat:.0f}s)"
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Health monitor error: {e}")

    def get_pending_requests(self, server: str | None = None) -> list[PendingRequest]:
        """Get all pending requests, optionally filtered by server."""
        result: list[PendingRequest] = []
        for name, managed in self._clients.items():
            if server and name != server:
                continue
            result.extend(managed.pending_requests.values())
        return result

    def get_request_state(self, pending: PendingRequest) -> RequestState:
        """Determine current state of a pending request."""
        now = time.time()
        elapsed = now - pending.started_at
        heartbeat_age = now - pending.last_heartbeat

        if pending.future.done():
            if pending.future.cancelled():
                return RequestState.CANCELLED
            return RequestState.COMPLETED
        if elapsed * 1000 > pending.timeout_ms:
            return RequestState.TIMEOUT
        if heartbeat_age > HEARTBEAT_STALL_THRESHOLD:
            return RequestState.STALLED
        if heartbeat_age > HEARTBEAT_WARN_THRESHOLD:
            return RequestState.ACTIVE  # Still active but slow
        return RequestState.PENDING

    async def cancel_request(
        self, request_id: str, force: bool = False
    ) -> tuple[str, str, bool, float | None]:
        """
        Cancel a pending request.

        Args:
            request_id: Format "server_name::local_id"
            force: Force cancel even if heartbeat is recent

        Returns:
            (status, message, was_stalled, elapsed_seconds)
            - status: "cancelled", "not_found", "already_complete", "refused"
        """
        # Parse request_id format "server_name::local_id"
        if "::" not in request_id:
            return (
                "not_found",
                f"Invalid request_id format: {request_id}",
                False,
                None,
            )

        server_name, local_id_str = request_id.rsplit("::", 1)
        try:
            local_id = int(local_id_str)
        except ValueError:
            return ("not_found", f"Invalid local_id: {local_id_str}", False, None)

        managed = self._clients.get(server_name)
        if not managed:
            return ("not_found", f"Server not found: {server_name}", False, None)

        pending = managed.pending_requests.get(local_id)
        if not pending:
            return ("not_found", f"Request not found: {request_id}", False, None)

        if pending.future.done():
            return ("already_complete", "Request already completed", False, None)

        now = time.time()
        elapsed = now - pending.started_at
        heartbeat_age = now - pending.last_heartbeat
        was_stalled = heartbeat_age > HEARTBEAT_STALL_THRESHOLD

        # Safety check: refuse to cancel healthy long-running requests unless forced
        if not force and not was_stalled and elapsed < pending.timeout_ms / 1000:
            return (
                "refused",
                f"Request is healthy (heartbeat {heartbeat_age:.0f}s ago). "
                f"Use force=true to cancel anyway.",
                False,
                elapsed,
            )

        # Cancel the request
        pending.future.cancel()
        managed.pending_requests.pop(local_id, None)
        managed.status.pending_request_count = len(managed.pending_requests)
        logger.info(
            f"Cancelled request {request_id} (stalled={was_stalled}, elapsed={elapsed:.1f}s)"
        )

        return ("cancelled", "Request cancelled successfully", was_stalled, elapsed)

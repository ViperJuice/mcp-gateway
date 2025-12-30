"""Shared pytest fixtures for MCP Gateway tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pmcp.policy.policy import PolicyManager
from pmcp.types import (
    McpServerConfig,
    ResolvedServerConfig,
    RiskHint,
    ServerStatus,
    ServerStatusEnum,
    ToolInfo,
)


# === Sample Data Factories ===


def create_tool_info(
    server_name: str = "test-server",
    tool_name: str = "test_tool",
    description: str = "A test tool",
    risk_hint: RiskHint = RiskHint.LOW,
    tags: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
) -> ToolInfo:
    """Factory for creating ToolInfo objects."""
    return ToolInfo(
        tool_id=f"{server_name}::{tool_name}",
        server_name=server_name,
        tool_name=tool_name,
        description=description,
        short_description=description[:100] if len(description) > 100 else description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        tags=tags or [server_name],
        risk_hint=risk_hint,
    )


def create_server_status(
    name: str = "test-server",
    status: ServerStatusEnum = ServerStatusEnum.ONLINE,
    tool_count: int = 5,
    last_error: str | None = None,
) -> ServerStatus:
    """Factory for creating ServerStatus objects."""
    return ServerStatus(
        name=name,
        status=status,
        tool_count=tool_count,
        last_error=last_error,
        last_connected_at=1234567890.0 if status == ServerStatusEnum.ONLINE else None,
    )


def create_server_config(
    name: str = "test-server",
    command: str = "echo",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> ResolvedServerConfig:
    """Factory for creating ResolvedServerConfig objects."""
    return ResolvedServerConfig(
        name=name,
        source="project",
        config=McpServerConfig(
            command=command,
            args=args or [],
            env=env,
        ),
    )


# === Sample Data Fixtures ===


@pytest.fixture
def sample_tools() -> list[ToolInfo]:
    """Sample list of tools for testing."""
    return [
        create_tool_info(
            server_name="github",
            tool_name="create_issue",
            description="Create a new issue in a GitHub repository",
            risk_hint=RiskHint.HIGH,
            tags=["github", "git", "issue"],
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body"},
                },
                "required": ["title"],
            },
        ),
        create_tool_info(
            server_name="github",
            tool_name="list_issues",
            description="List issues in a repository",
            risk_hint=RiskHint.LOW,
            tags=["github", "git", "search"],
        ),
        create_tool_info(
            server_name="jira",
            tool_name="search_issues",
            description="Search for Jira issues using JQL",
            risk_hint=RiskHint.LOW,
            tags=["jira", "search"],
        ),
        create_tool_info(
            server_name="filesystem",
            tool_name="delete_file",
            description="Delete a file from the filesystem",
            risk_hint=RiskHint.HIGH,
            tags=["fs", "file", "delete"],
        ),
        create_tool_info(
            server_name="filesystem",
            tool_name="read_file",
            description="Read contents of a file",
            risk_hint=RiskHint.LOW,
            tags=["fs", "file", "read"],
        ),
    ]


@pytest.fixture
def sample_server_statuses() -> list[ServerStatus]:
    """Sample list of server statuses for testing."""
    return [
        create_server_status("github", ServerStatusEnum.ONLINE, 10),
        create_server_status("jira", ServerStatusEnum.ONLINE, 5),
        create_server_status(
            "filesystem", ServerStatusEnum.OFFLINE, 0, "Connection refused"
        ),
    ]


@pytest.fixture
def sample_server_configs() -> list[ResolvedServerConfig]:
    """Sample list of server configs for testing."""
    return [
        create_server_config(
            "github",
            "npx",
            ["-y", "@modelcontextprotocol/server-github"],
            {"GITHUB_TOKEN": "test-token"},
        ),
        create_server_config(
            "filesystem",
            "npx",
            ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        ),
    ]


# === Mock Client Manager ===


class MockClientManager:
    """Mock client manager for testing gateway tools."""

    def __init__(
        self,
        tools: list[ToolInfo] | None = None,
        server_statuses: list[ServerStatus] | None = None,
    ) -> None:
        self._tools = {t.tool_id: t for t in (tools or [])}
        self._server_statuses = {s.name: s for s in (server_statuses or [])}
        self._online_servers: set[str] = {
            s.name
            for s in (server_statuses or [])
            if s.status == ServerStatusEnum.ONLINE
        }
        self._revision_id = "test-rev-001"
        self._last_refresh_ts = 1234567890.0
        self._call_tool_response: Any = {
            "content": [{"type": "text", "text": "result"}]
        }
        self._call_tool_error: Exception | None = None

    def get_all_tools(self) -> list[ToolInfo]:
        return list(self._tools.values())

    def get_tool(self, tool_id: str) -> ToolInfo | None:
        return self._tools.get(tool_id)

    def is_server_online(self, name: str) -> bool:
        return name in self._online_servers

    def set_server_online(self, name: str, online: bool = True) -> None:
        if online:
            self._online_servers.add(name)
        else:
            self._online_servers.discard(name)

    def get_server_status(self, name: str) -> ServerStatus | None:
        return self._server_statuses.get(name)

    def get_all_server_statuses(self) -> list[ServerStatus]:
        return list(self._server_statuses.values())

    def get_registry_meta(self) -> tuple[str, float]:
        return (self._revision_id, self._last_refresh_ts)

    async def call_tool(
        self, tool_id: str, args: dict[str, Any], timeout_ms: int
    ) -> Any:
        if self._call_tool_error:
            raise self._call_tool_error
        return self._call_tool_response

    async def refresh(self, configs: list[Any]) -> list[str]:
        return []

    def set_call_tool_response(self, response: Any) -> None:
        """Set the response for call_tool."""
        self._call_tool_response = response
        self._call_tool_error = None

    def set_call_tool_error(self, error: Exception) -> None:
        """Set an error to raise from call_tool."""
        self._call_tool_error = error


@pytest.fixture
def mock_client_manager(
    sample_tools: list[ToolInfo],
    sample_server_statuses: list[ServerStatus],
) -> MockClientManager:
    """Create a mock client manager with sample data."""
    return MockClientManager(
        tools=sample_tools,
        server_statuses=sample_server_statuses,
    )


# === Mock Policy Manager ===


@pytest.fixture
def mock_policy_manager() -> PolicyManager:
    """Create a permissive policy manager for testing."""
    return PolicyManager()


@pytest.fixture
def strict_policy_manager() -> PolicyManager:
    """Create a strict policy manager that blocks high-risk tools."""
    manager = PolicyManager()
    manager._tool_denylist = ["*::delete_*", "*::execute_*"]
    return manager


# === Temporary Directory Fixtures ===


@pytest.fixture
def temp_dir() -> Path:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_config_dir(temp_dir: Path) -> Path:
    """Create a temporary directory with sample config files."""
    config_dir = temp_dir / ".claude"
    config_dir.mkdir(parents=True)

    # Create sample mcp.json
    mcp_json = config_dir / "mcp.json"
    mcp_json.write_text(
        """{
  "mcpServers": {
    "test-server": {
      "command": "echo",
      "args": ["hello"]
    }
  }
}"""
    )

    # Create sample policy file
    policy_yaml = config_dir / "gateway-policy.yaml"
    policy_yaml.write_text(
        """servers:
  allowlist:
    - "*"
  denylist: []

tools:
  allowlist:
    - "*"
  denylist:
    - "*::delete_*"

output:
  max_size_bytes: 50000
  max_tokens: 10000

redaction:
  enabled: true
  patterns:
    - "sk-[a-zA-Z0-9]{48}"
"""
    )

    return config_dir


# === Async Mock Helpers ===


@pytest.fixture
def mock_aiohttp_session() -> MagicMock:
    """Create a mock aiohttp ClientSession."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


def create_mock_response(
    status: int = 200,
    json_data: dict[str, Any] | None = None,
    text_data: str = "",
) -> MagicMock:
    """Create a mock aiohttp response."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data or {})
    response.text = AsyncMock(return_value=text_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)
    return response


# === Process Mock Helpers ===


def create_mock_process(
    returncode: int | None = None,
    stdout_data: bytes = b"",
    stderr_data: bytes = b"",
) -> MagicMock:
    """Create a mock asyncio subprocess."""
    process = MagicMock()
    process.returncode = returncode
    process.pid = 12345
    process.stdout = MagicMock()
    process.stdout.readline = AsyncMock(return_value=stdout_data)
    process.stdout.read = AsyncMock(return_value=stdout_data)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr_data)
    process.stdin = MagicMock()
    process.stdin.write = MagicMock()
    process.stdin.drain = AsyncMock()
    process.wait = AsyncMock(return_value=returncode)
    process.terminate = MagicMock()
    process.kill = MagicMock()
    return process

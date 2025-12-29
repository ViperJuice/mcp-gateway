"""Tests for ClientManager."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_gateway.client.manager import (
    ClientManager,
    ManagedClient,
    PendingRequest,
    _extract_tags,
    _infer_risk_hint,
    _truncate_description,
)
from mcp_gateway.types import (
    RiskHint,
    ServerStatus,
    ServerStatusEnum,
)


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_infer_risk_hint_low(self) -> None:
        """Test low risk hint inference."""
        assert _infer_risk_hint("read_file", "Read a file") == RiskHint.LOW
        assert _infer_risk_hint("list_items", "List all items") == RiskHint.LOW
        assert _infer_risk_hint("search", "Search for content") == RiskHint.LOW

    def test_infer_risk_hint_high(self) -> None:
        """Test high risk hint inference."""
        assert _infer_risk_hint("delete_file", "Delete a file") == RiskHint.HIGH
        assert _infer_risk_hint("execute_command", "Run a command") == RiskHint.HIGH
        assert _infer_risk_hint("write_data", "Write data to disk") == RiskHint.HIGH

    def test_infer_risk_hint_medium(self) -> None:
        """Test medium risk hint inference (default)."""
        assert _infer_risk_hint("process_item", "Process an item") == RiskHint.MEDIUM

    def test_extract_tags(self) -> None:
        """Test tag extraction."""
        tags = _extract_tags("github", "create_issue", "Create a GitHub issue")
        assert "github" in tags

        tags = _extract_tags("fs", "read_file", "Read a file from the filesystem")
        assert "fs" in tags
        assert "file" in tags

    def test_truncate_description(self) -> None:
        """Test description truncation."""
        short = "Short description"
        assert _truncate_description(short) == short

        long = "A" * 200
        truncated = _truncate_description(long, max_length=100)
        assert len(truncated) == 100
        assert truncated.endswith("...")

        assert _truncate_description("") == ""


class TestClientManager:
    """Tests for ClientManager class."""

    @pytest.fixture
    def manager(self) -> ClientManager:
        """Create a ClientManager instance."""
        return ClientManager(max_tools_per_server=100)

    def test_init(self, manager: ClientManager) -> None:
        """Test ClientManager initialization."""
        assert manager._clients == {}
        assert manager._tools == {}
        assert manager._servers == {}
        assert manager._max_tools_per_server == 100

    def test_get_tool_not_found(self, manager: ClientManager) -> None:
        """Test get_tool returns None for unknown tools."""
        assert manager.get_tool("unknown::tool") is None

    def test_get_all_tools_empty(self, manager: ClientManager) -> None:
        """Test get_all_tools returns empty list initially."""
        assert manager.get_all_tools() == []

    def test_get_server_status_not_found(self, manager: ClientManager) -> None:
        """Test get_server_status returns None for unknown servers."""
        assert manager.get_server_status("unknown") is None

    def test_is_server_online_false(self, manager: ClientManager) -> None:
        """Test is_server_online returns False for unknown servers."""
        assert manager.is_server_online("unknown") is False

    def test_get_registry_meta(self, manager: ClientManager) -> None:
        """Test get_registry_meta returns revision and timestamp."""
        revision_id, last_refresh_ts = manager.get_registry_meta()
        assert revision_id.startswith("rev-")
        assert last_refresh_ts > 0


class TestDisconnectAll:
    """Tests for disconnect_all method."""

    @pytest.fixture
    def manager_with_client(self) -> tuple[ClientManager, ManagedClient]:
        """Create a ClientManager with a mock client."""
        manager = ClientManager()

        # Create mock process
        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)

        # Create mock status
        status = ServerStatus(
            name="test",
            status=ServerStatusEnum.ONLINE,
            tool_count=5,
        )

        # Create managed client
        managed = ManagedClient(
            config=MagicMock(),
            process=mock_process,
            status=status,
        )
        managed.read_task = None

        manager._clients["test"] = managed
        manager._servers["test"] = status

        return manager, managed

    @pytest.mark.asyncio
    async def test_disconnect_all_terminates_process(
        self, manager_with_client: tuple[ClientManager, ManagedClient]
    ) -> None:
        """Test that disconnect_all terminates processes."""
        manager, managed = manager_with_client

        await manager.disconnect_all()

        managed.process.terminate.assert_called_once()
        assert manager._clients == {}
        assert manager._servers == {}

    @pytest.mark.asyncio
    async def test_disconnect_all_cancels_pending_requests(
        self, manager_with_client: tuple[ClientManager, ManagedClient]
    ) -> None:
        """Test that disconnect_all cancels pending requests."""
        manager, managed = manager_with_client

        # Add pending request using PendingRequest
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        pending = PendingRequest(
            request_id=1,
            server_name="test",
            tool_id="test::tool",
            started_at=time.time(),
            last_heartbeat=time.time(),
            timeout_ms=30000,
            future=future,
        )
        managed.pending_requests[1] = pending

        await manager.disconnect_all()

        assert future.cancelled()
        assert managed.pending_requests == {}

    @pytest.mark.asyncio
    async def test_disconnect_all_handles_timeout(
        self, manager_with_client: tuple[ClientManager, ManagedClient]
    ) -> None:
        """Test that disconnect_all kills process on timeout."""
        manager, managed = manager_with_client

        # Make wait timeout
        managed.process.wait = AsyncMock(side_effect=asyncio.TimeoutError())

        await manager.disconnect_all()

        managed.process.terminate.assert_called_once()
        managed.process.kill.assert_called_once()


class TestCallTool:
    """Tests for call_tool method."""

    @pytest.fixture
    def manager_with_tool(self) -> ClientManager:
        """Create a ClientManager with a mock tool."""
        manager = ClientManager()

        # Add a tool
        from mcp_gateway.types import ToolInfo

        tool = ToolInfo(
            tool_id="test::echo",
            server_name="test",
            tool_name="echo",
            description="Echo input",
            short_description="Echo input",
            input_schema={"type": "object"},
            tags=["test"],
            risk_hint=RiskHint.LOW,
        )
        manager._tools["test::echo"] = tool

        return manager

    @pytest.mark.asyncio
    async def test_call_tool_unknown_tool(
        self, manager_with_tool: ClientManager
    ) -> None:
        """Test call_tool raises for unknown tools."""
        with pytest.raises(ValueError, match="Unknown tool"):
            await manager_with_tool.call_tool("unknown::tool", {})

    @pytest.mark.asyncio
    async def test_call_tool_server_not_connected(
        self, manager_with_tool: ClientManager
    ) -> None:
        """Test call_tool raises when server not connected."""
        with pytest.raises(RuntimeError, match="not connected"):
            await manager_with_tool.call_tool("test::echo", {})


class TestServerHealthTracking:
    """Tests for server health tracking."""

    @pytest.mark.asyncio
    async def test_read_stdout_marks_server_offline_on_eof(self) -> None:
        """Test that _read_stdout marks server offline when EOF received."""
        manager = ClientManager()

        # Create mock status
        status = ServerStatus(
            name="test",
            status=ServerStatusEnum.ONLINE,
            tool_count=5,
        )

        # Create mock process with empty stdout (EOF)
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")

        mock_process = MagicMock()
        mock_process.stdout = mock_stdout

        managed = ManagedClient(
            config=MagicMock(),
            process=mock_process,
            status=status,
        )

        # Run _read_stdout
        await manager._read_stdout("test", managed)

        # Status should be ERROR after EOF
        assert status.status == ServerStatusEnum.ERROR
        assert status.last_error == "Server process exited"

    @pytest.mark.asyncio
    async def test_read_stdout_cancels_pending_on_eof(self) -> None:
        """Test that _read_stdout cancels pending requests on EOF."""
        manager = ClientManager()

        status = ServerStatus(
            name="test",
            status=ServerStatusEnum.ONLINE,
            tool_count=5,
        )

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")

        mock_process = MagicMock()
        mock_process.stdout = mock_stdout

        managed = ManagedClient(
            config=MagicMock(),
            process=mock_process,
            status=status,
        )

        # Add pending request using PendingRequest
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        pending = PendingRequest(
            request_id=1,
            server_name="test",
            tool_id="test::tool",
            started_at=time.time(),
            last_heartbeat=time.time(),
            timeout_ms=30000,
            future=future,
        )
        managed.pending_requests[1] = pending

        await manager._read_stdout("test", managed)

        # Request should be failed with ConnectionError
        assert future.done()
        with pytest.raises(ConnectionError):
            future.result()

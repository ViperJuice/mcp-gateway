"""Tests for ClientManager."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from pmcp.client.manager import (
    ClientManager,
    ManagedClient,
    PendingRequest,
    _extract_tags,
    _infer_risk_hint,
    _truncate_description,
)
from pmcp.types import (
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
        from pmcp.types import ToolInfo

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


class TestResourcesAndPrompts:
    """Tests for resource and prompt support."""

    @pytest.fixture
    def manager(self) -> ClientManager:
        """Create a ClientManager instance."""
        return ClientManager()

    def test_init_has_resources_and_prompts(self, manager: ClientManager) -> None:
        """Test ClientManager initializes with empty resources and prompts."""
        assert manager._resources == {}
        assert manager._prompts == {}

    def test_get_resource_not_found(self, manager: ClientManager) -> None:
        """Test get_resource returns None for unknown resources."""
        assert manager.get_resource("unknown::resource") is None

    def test_get_all_resources_empty(self, manager: ClientManager) -> None:
        """Test get_all_resources returns empty list initially."""
        assert manager.get_all_resources() == []

    def test_get_prompt_info_not_found(self, manager: ClientManager) -> None:
        """Test get_prompt_info returns None for unknown prompts."""
        assert manager.get_prompt_info("unknown::prompt") is None

    def test_get_all_prompts_empty(self, manager: ClientManager) -> None:
        """Test get_all_prompts returns empty list initially."""
        assert manager.get_all_prompts() == []

    @pytest.fixture
    def manager_with_resources(self) -> ClientManager:
        """Create a ClientManager with test resources."""
        from pmcp.types import ResourceInfo

        manager = ClientManager()

        resource = ResourceInfo(
            resource_id="test::file:///test.txt",
            server_name="test",
            uri="file:///test.txt",
            name="test.txt",
            description="A test file",
            mime_type="text/plain",
        )
        manager._resources["test::file:///test.txt"] = resource

        return manager

    @pytest.fixture
    def manager_with_prompts(self) -> ClientManager:
        """Create a ClientManager with test prompts."""
        from pmcp.types import PromptArgumentInfo, PromptInfo

        manager = ClientManager()

        prompt = PromptInfo(
            prompt_id="test::greeting",
            server_name="test",
            name="greeting",
            description="A greeting prompt",
            arguments=[
                PromptArgumentInfo(
                    name="name",
                    description="Name to greet",
                    required=True,
                )
            ],
        )
        manager._prompts["test::greeting"] = prompt

        return manager

    def test_get_resource_found(self, manager_with_resources: ClientManager) -> None:
        """Test get_resource returns resource info."""
        resource = manager_with_resources.get_resource("test::file:///test.txt")
        assert resource is not None
        assert resource.name == "test.txt"
        assert resource.mime_type == "text/plain"

    def test_get_all_resources(self, manager_with_resources: ClientManager) -> None:
        """Test get_all_resources returns all resources."""
        resources = manager_with_resources.get_all_resources()
        assert len(resources) == 1
        assert resources[0].uri == "file:///test.txt"

    def test_get_prompt_info_found(self, manager_with_prompts: ClientManager) -> None:
        """Test get_prompt_info returns prompt info."""
        prompt = manager_with_prompts.get_prompt_info("test::greeting")
        assert prompt is not None
        assert prompt.name == "greeting"
        assert prompt.arguments is not None
        assert len(prompt.arguments) == 1

    def test_get_all_prompts(self, manager_with_prompts: ClientManager) -> None:
        """Test get_all_prompts returns all prompts."""
        prompts = manager_with_prompts.get_all_prompts()
        assert len(prompts) == 1
        assert prompts[0].name == "greeting"

    @pytest.mark.asyncio
    async def test_read_resource_unknown(
        self, manager_with_resources: ClientManager
    ) -> None:
        """Test read_resource raises for unknown resources."""
        with pytest.raises(ValueError, match="Unknown resource"):
            await manager_with_resources.read_resource("unknown::resource")

    @pytest.mark.asyncio
    async def test_read_resource_server_not_connected(
        self, manager_with_resources: ClientManager
    ) -> None:
        """Test read_resource raises when server not connected."""
        with pytest.raises(RuntimeError, match="not connected"):
            await manager_with_resources.read_resource("test::file:///test.txt")

    @pytest.mark.asyncio
    async def test_get_prompt_unknown(
        self, manager_with_prompts: ClientManager
    ) -> None:
        """Test get_prompt raises for unknown prompts."""
        with pytest.raises(ValueError, match="Unknown prompt"):
            await manager_with_prompts.get_prompt("unknown::prompt")

    @pytest.mark.asyncio
    async def test_get_prompt_server_not_connected(
        self, manager_with_prompts: ClientManager
    ) -> None:
        """Test get_prompt raises when server not connected."""
        with pytest.raises(RuntimeError, match="not connected"):
            await manager_with_prompts.get_prompt("test::greeting")


class TestParallelConnections:
    """Tests for parallel connection behavior."""

    @pytest.mark.asyncio
    async def test_connect_all_empty_list(self) -> None:
        """Test connect_all with empty config list."""
        manager = ClientManager()
        errors = await manager.connect_all([])
        assert errors == []

    @pytest.mark.asyncio
    async def test_connect_all_parallel_execution(self) -> None:
        """Test that connect_all runs connections in parallel."""
        manager = ClientManager()
        call_times: list[float] = []

        async def mock_connect(config: MagicMock) -> None:
            call_times.append(time.time())
            await asyncio.sleep(0.1)  # Simulate connection time

        # Patch the connection method
        manager._connect_server = mock_connect  # type: ignore[method-assign]

        # Create mock configs
        configs = [MagicMock(name=f"server{i}") for i in range(3)]

        start = time.time()
        await manager.connect_all(configs, retry=False)
        elapsed = time.time() - start

        # If parallel, should complete in ~0.1s, not ~0.3s
        assert elapsed < 0.2, f"Expected parallel execution, took {elapsed}s"
        assert len(call_times) == 3

    @pytest.mark.asyncio
    async def test_connect_all_collects_errors(self) -> None:
        """Test that connect_all collects errors from failed connections."""
        manager = ClientManager()

        async def mock_connect(config: MagicMock) -> None:
            if getattr(config, "_server_name", "") == "fail":
                raise RuntimeError("Connection failed")

        manager._connect_server = mock_connect  # type: ignore[method-assign]

        # Create configs with server names
        configs = []
        for name in ["success", "fail", "success2"]:
            config = MagicMock()
            config._server_name = name
            config.name = name
            configs.append(config)

        errors = await manager.connect_all(configs, retry=False)
        assert len(errors) == 1
        assert "fail" in errors[0]
        assert "Connection failed" in errors[0]


class TestConnectionRetry:
    """Tests for connection retry behavior."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self) -> None:
        """Test that retry succeeds after initial failure."""
        manager = ClientManager()
        attempts = 0

        async def mock_connect(config: MagicMock) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError("Transient failure")

        manager._connect_server = mock_connect  # type: ignore[method-assign]

        config = MagicMock(name="retry-server")
        await manager._connect_with_retry(config)

        assert attempts == 2  # First failed, second succeeded

    @pytest.mark.asyncio
    async def test_retry_exhausts_all_attempts(self) -> None:
        """Test that retry raises after all attempts fail."""
        manager = ClientManager()
        attempts = 0

        async def mock_connect(config: MagicMock) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError(f"Failure {attempts}")

        manager._connect_server = mock_connect  # type: ignore[method-assign]

        config = MagicMock(name="always-fail")

        with pytest.raises(RuntimeError, match="Failure 3"):
            await manager._connect_with_retry(config)

        assert attempts == 3  # All retries exhausted

    @pytest.mark.asyncio
    async def test_retry_disabled(self) -> None:
        """Test that retry can be disabled."""
        manager = ClientManager()
        attempts = 0

        async def mock_connect(config: MagicMock) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Failure")

        manager._connect_server = mock_connect  # type: ignore[method-assign]

        configs = [MagicMock(name="no-retry")]
        errors = await manager.connect_all(configs, retry=False)

        assert attempts == 1  # No retry
        assert len(errors) == 1

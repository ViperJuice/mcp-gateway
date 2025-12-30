"""Tests for GatewayServer lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pmcp.server import GatewayServer


class TestGatewayServerInit:
    """Tests for GatewayServer initialization."""

    def test_init_defaults(self) -> None:
        """Test GatewayServer initializes with defaults."""
        server = GatewayServer()

        assert server._project_root is None
        assert server._custom_config_path is None
        assert server._cache_dir == Path(".mcp-gateway")
        assert server._server is None
        assert server._capability_summary == ""

    def test_init_with_paths(self, tmp_path: Path) -> None:
        """Test GatewayServer initializes with custom paths."""
        project_root = tmp_path / "project"
        config_path = tmp_path / "config.json"
        cache_dir = tmp_path / "cache"

        server = GatewayServer(
            project_root=project_root,
            custom_config_path=config_path,
            cache_dir=cache_dir,
        )

        assert server._project_root == project_root
        assert server._custom_config_path == config_path
        assert server._cache_dir == cache_dir


class TestGatewayServerShutdown:
    """Tests for GatewayServer shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_clients(self) -> None:
        """Test that shutdown disconnects all clients."""
        server = GatewayServer()

        # Mock the client manager
        server._client_manager = MagicMock()
        server._client_manager.disconnect_all = AsyncMock()

        await server.shutdown()

        server._client_manager.disconnect_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_timeout(self) -> None:
        """Test that shutdown handles timeout gracefully."""
        server = GatewayServer()

        # Mock client manager that takes too long
        async def slow_disconnect() -> None:
            await asyncio.sleep(20)  # Longer than timeout

        server._client_manager = MagicMock()
        server._client_manager.disconnect_all = slow_disconnect

        # Should complete without error (timeout is 10s internally)
        # We'll just verify it doesn't raise
        await asyncio.wait_for(server.shutdown(), timeout=15)

    @pytest.mark.asyncio
    async def test_shutdown_handles_error(self) -> None:
        """Test that shutdown handles errors gracefully."""
        server = GatewayServer()

        # Mock client manager that raises
        server._client_manager = MagicMock()
        server._client_manager.disconnect_all = AsyncMock(
            side_effect=RuntimeError("Disconnect failed")
        )

        # Should not raise
        await server.shutdown()


class TestGatewayServerHandlers:
    """Tests for GatewayServer handler registration."""

    def test_create_server_registers_handlers(self) -> None:
        """Test that _create_server registers handlers."""
        server = GatewayServer()
        server._create_server(instructions="Test instructions")

        assert server._server is not None
        assert server._server.name == "mcp-gateway"

    def test_create_server_with_instructions(self) -> None:
        """Test that _create_server passes instructions."""
        server = GatewayServer()
        instructions = "Test capability summary"
        server._create_server(instructions=instructions)

        # Server should be created
        assert server._server is not None


class TestGatewayServerIntegration:
    """Integration tests for GatewayServer (requires mocking)."""

    @pytest.mark.asyncio
    async def test_initialize_no_configs(self) -> None:
        """Test initialize with no server configs."""
        with patch("pmcp.server.load_configs", return_value=[]):
            with patch("pmcp.server.load_manifest") as mock_manifest:
                mock_manifest.return_value = MagicMock()
                mock_manifest.return_value.get_auto_start_servers = MagicMock(
                    return_value=[]
                )

                with patch("pmcp.server.load_descriptions_cache", return_value=None):
                    with patch(
                        "pmcp.server.generate_capability_summary"
                    ) as mock_summary:
                        mock_summary.return_value = "No tools available"

                        server = GatewayServer()
                        await server.initialize()

                        assert server._server is not None
                        assert server._capability_summary == "No tools available"

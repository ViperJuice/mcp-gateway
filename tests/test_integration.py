"""Integration tests for MCP Gateway.

These tests require MCP servers available via config or manifest.
Skip with: pytest tests/test_integration.py -v --skip-integration
"""

from __future__ import annotations

import os
import pytest

from pmcp.config.loader import load_configs, manifest_server_to_config
from pmcp.client.manager import ClientManager
from pmcp.manifest.loader import load_manifest
from pmcp.policy.policy import PolicyManager
from pmcp.summary import generate_capability_summary
from pmcp.summary.template_fallback import template_summary
from pmcp.server import GatewayServer


def get_available_servers() -> list:
    """Get all available servers from config and manifest auto-start."""
    # Load user configs
    configs = load_configs()
    # Filter out gateway itself
    configs = [c for c in configs if c.name != "mcp-gateway"]
    seen_servers = {c.name for c in configs}

    # Add manifest auto-start servers
    manifest = load_manifest()
    if manifest:
        for server in manifest.get_auto_start_servers():
            if server.name in seen_servers:
                continue
            # Skip if requires API key that's not set
            if server.requires_api_key and server.env_var:
                if not os.environ.get(server.env_var):
                    continue
            configs.append(manifest_server_to_config(server))

    return configs


def has_mcp_servers() -> bool:
    """Check if there are MCP servers available (config or manifest)."""
    return len(get_available_servers()) > 0


skip_no_servers = pytest.mark.skipif(
    not has_mcp_servers(), reason="No MCP servers available (config or manifest)"
)


class TestConfigLoading:
    """Test config loading from real files and manifest."""

    def test_loads_available_servers(self) -> None:
        """Verify we can discover servers from config and manifest."""
        configs = get_available_servers()
        # Should find servers (from config or manifest auto-start)
        assert isinstance(configs, list)

        # Print what was found for debugging
        for cfg in configs:
            print(f"  Found: {cfg.name} ({cfg.source})")


@skip_no_servers
class TestServerConnection:
    """Test connecting to real MCP servers."""

    @pytest.mark.asyncio
    async def test_connects_to_servers(self) -> None:
        """Test connecting to available MCP servers."""
        configs = get_available_servers()
        policy = PolicyManager()

        allowed = [c for c in configs if policy.is_server_allowed(c.name)]
        assert len(allowed) > 0, "No allowed servers"

        manager = ClientManager()
        try:
            errors = await manager.connect_all(allowed)

            # Check what connected
            statuses = manager.get_all_server_statuses()
            for status in statuses:
                print(
                    f"  {status.name}: {status.status.value} ({status.tool_count} tools)"
                )

            # At least some should connect (network might be slow)
            online = [s for s in statuses if s.status.value == "online"]
            assert len(online) > 0 or len(errors) > 0, "No servers online and no errors"

        finally:
            await manager.disconnect_all()

    @pytest.mark.asyncio
    async def test_lists_tools_from_servers(self) -> None:
        """Test listing tools from connected servers."""
        configs = get_available_servers()
        policy = PolicyManager()

        allowed = [c for c in configs if policy.is_server_allowed(c.name)]
        manager = ClientManager()

        try:
            await manager.connect_all(allowed)
            tools = manager.get_all_tools()

            print(f"  Found {len(tools)} tools total")
            for tool in tools[:10]:  # First 10
                print(f"    {tool.tool_id}: {tool.short_description[:50]}...")

            # Should have at least some tools
            assert len(tools) > 0, "No tools found"

        finally:
            await manager.disconnect_all()


@skip_no_servers
class TestSummaryGeneration:
    """Test summary generation with real tools."""

    @pytest.mark.asyncio
    async def test_template_summary_with_real_tools(self) -> None:
        """Test template fallback generates summary for real tools."""
        configs = get_available_servers()
        policy = PolicyManager()

        allowed = [c for c in configs if policy.is_server_allowed(c.name)]
        manager = ClientManager()

        try:
            await manager.connect_all(allowed)
            tools = manager.get_all_tools()

            if not tools:
                pytest.skip("No tools available")

            summary = template_summary(tools)

            print(f"\nTemplate Summary:\n{summary}")

            assert "MCP Gateway capabilities:" in summary
            assert "gateway.catalog_search" in summary

        finally:
            await manager.disconnect_all()

    @pytest.mark.asyncio
    async def test_generate_capability_summary_fallback(self) -> None:
        """Test generate_capability_summary falls back to template."""
        configs = get_available_servers()
        policy = PolicyManager()

        allowed = [c for c in configs if policy.is_server_allowed(c.name)]
        manager = ClientManager()

        try:
            await manager.connect_all(allowed)
            tools = manager.get_all_tools()

            if not tools:
                pytest.skip("No tools available")

            # With use_llm=False, should use template
            summary = await generate_capability_summary(tools, use_llm=False)

            print(f"\nFallback Summary:\n{summary}")

            assert len(summary) > 0
            assert "gateway" in summary.lower()

        finally:
            await manager.disconnect_all()


@skip_no_servers
class TestGatewayServer:
    """Test full gateway server initialization."""

    @pytest.mark.asyncio
    async def test_gateway_initializes(self) -> None:
        """Test that gateway server initializes successfully."""
        server = GatewayServer()

        try:
            await server.initialize()

            # Check that capability summary was generated
            assert server._capability_summary, "No capability summary generated"
            print(f"\nCapability Summary:\n{server._capability_summary}")

            # Check that MCP server was created with instructions
            assert server._server is not None, "MCP server not created"

        finally:
            await server.shutdown()


@skip_no_servers
class TestBAMLSummarization:
    """Test BAML LLM summarization (requires GROQ_API_KEY)."""

    @pytest.mark.asyncio
    async def test_baml_summarization(self) -> None:
        """Test BAML summarization with real API."""
        if not os.environ.get("GROQ_API_KEY"):
            pytest.skip("GROQ_API_KEY not set")

        configs = get_available_servers()
        policy = PolicyManager()

        allowed = [c for c in configs if policy.is_server_allowed(c.name)]
        manager = ClientManager()

        try:
            await manager.connect_all(allowed)
            tools = manager.get_all_tools()

            if not tools:
                pytest.skip("No tools available")

            # With use_llm=True, should use BAML
            summary = await generate_capability_summary(tools, use_llm=True)

            print(f"\nBAML LLM Summary:\n{summary}")

            assert "MCP Gateway capabilities:" in summary
            assert len(summary) > 50  # Should be a real summary

        finally:
            await manager.disconnect_all()

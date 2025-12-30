"""Tests for GatewayServer MCP handlers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pmcp.server import GatewayServer
from pmcp.types import (
    PromptArgumentInfo,
    PromptInfo,
    ResourceInfo,
    ServerStatus,
    ServerStatusEnum,
)


class TestGatewayServerInit:
    """Tests for GatewayServer initialization."""

    def test_creates_with_defaults(self) -> None:
        """Test server creates with default values."""
        server = GatewayServer()
        assert server._project_root is None
        assert server._custom_config_path is None
        assert server._server is None

    def test_creates_with_custom_paths(self, tmp_path: Path) -> None:
        """Test server creates with custom paths."""
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("servers:\n  denylist: []\n")

        server = GatewayServer(
            project_root=tmp_path,
            policy_path=policy_file,
        )
        assert server._project_root == tmp_path


class TestResourceHandlers:
    """Tests for resource MCP handlers."""

    @pytest.fixture
    def server_with_resources(self) -> GatewayServer:
        """Create a server with mocked resources."""
        server = GatewayServer()

        # Mock resources in client manager
        server._client_manager._resources = {
            "test::file:///readme.md": ResourceInfo(
                resource_id="test::file:///readme.md",
                server_name="test",
                uri="file:///readme.md",
                name="README",
                description="Project readme",
                mime_type="text/markdown",
            ),
            "test::file:///secret.env": ResourceInfo(
                resource_id="test::file:///secret.env",
                server_name="test",
                uri="file:///secret.env",
                name="Secrets",
                description="Secret config",
                mime_type="text/plain",
            ),
        }

        return server

    def test_get_all_resources(self, server_with_resources: GatewayServer) -> None:
        """Test getting all resources."""
        resources = server_with_resources._client_manager.get_all_resources()
        assert len(resources) == 2

    def test_resource_policy_allows_by_default(
        self, server_with_resources: GatewayServer
    ) -> None:
        """Test that resources are allowed by default."""
        assert server_with_resources._policy_manager.is_resource_allowed(
            "test::file:///readme.md"
        )

    def test_resource_policy_blocks_denylist(self, tmp_path: Path) -> None:
        """Test that resources on denylist are blocked."""
        import json

        policy_file = tmp_path / "policy.json"
        policy_file.write_text(
            json.dumps({"resources": {"denylist": ["*::file:///*.env"]}})
        )

        server = GatewayServer(policy_path=policy_file)
        assert not server._policy_manager.is_resource_allowed(
            "test::file:///secret.env"
        )
        assert server._policy_manager.is_resource_allowed("test::file:///readme.md")


class TestPromptHandlers:
    """Tests for prompt MCP handlers."""

    @pytest.fixture
    def server_with_prompts(self) -> GatewayServer:
        """Create a server with mocked prompts."""
        server = GatewayServer()

        # Mock prompts in client manager
        server._client_manager._prompts = {
            "test::greeting": PromptInfo(
                prompt_id="test::greeting",
                server_name="test",
                name="greeting",
                description="Generate a greeting",
                arguments=[
                    PromptArgumentInfo(
                        name="name",
                        description="Name to greet",
                        required=True,
                    )
                ],
            ),
            "admin::dangerous": PromptInfo(
                prompt_id="admin::dangerous",
                server_name="admin",
                name="dangerous",
                description="A dangerous prompt",
                arguments=None,
            ),
        }

        return server

    def test_get_all_prompts(self, server_with_prompts: GatewayServer) -> None:
        """Test getting all prompts."""
        prompts = server_with_prompts._client_manager.get_all_prompts()
        assert len(prompts) == 2

    def test_prompt_policy_allows_by_default(
        self, server_with_prompts: GatewayServer
    ) -> None:
        """Test that prompts are allowed by default."""
        assert server_with_prompts._policy_manager.is_prompt_allowed("test::greeting")

    def test_prompt_policy_blocks_denylist(self, tmp_path: Path) -> None:
        """Test that prompts on denylist are blocked."""
        import json

        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps({"prompts": {"denylist": ["admin::*"]}}))

        server = GatewayServer(policy_path=policy_file)
        assert not server._policy_manager.is_prompt_allowed("admin::dangerous")
        assert server._policy_manager.is_prompt_allowed("test::greeting")


class TestServerCreation:
    """Tests for MCP server creation."""

    def test_create_server_with_instructions(self) -> None:
        """Test server creates with capability instructions."""
        server = GatewayServer()
        server._create_server(instructions="Test instructions")

        assert server._server is not None
        # Server is created, we just verify it's not None
        # (internal _instructions attribute may not be exposed)

    def test_setup_handlers_requires_server(self) -> None:
        """Test setup_handlers raises if server not initialized."""
        server = GatewayServer()

        with pytest.raises(RuntimeError, match="Server not initialized"):
            server._setup_handlers()


class TestPolicyIntegration:
    """Tests for policy integration with handlers."""

    def test_combined_policy_filters(self, tmp_path: Path) -> None:
        """Test that policy filters work together."""
        import json

        policy_file = tmp_path / "policy.json"
        policy_file.write_text(
            json.dumps(
                {
                    "servers": {"denylist": ["blocked-server"]},
                    "tools": {"denylist": ["*::delete_*"]},
                    "resources": {"denylist": ["*::*.env"]},
                    "prompts": {"denylist": ["admin::*"]},
                }
            )
        )

        server = GatewayServer(policy_path=policy_file)

        # Check all policies apply
        assert not server._policy_manager.is_server_allowed("blocked-server")
        assert not server._policy_manager.is_tool_allowed("github::delete_repo")
        assert not server._policy_manager.is_resource_allowed("test::file.env")
        assert not server._policy_manager.is_prompt_allowed("admin::dangerous")

        # Check allowed ones still work
        assert server._policy_manager.is_server_allowed("github")
        assert server._policy_manager.is_tool_allowed("github::create_issue")
        assert server._policy_manager.is_resource_allowed("test::readme.md")
        assert server._policy_manager.is_prompt_allowed("test::greeting")


class TestServerStatus:
    """Tests for server status tracking."""

    def test_server_status_fields(self) -> None:
        """Test ServerStatus has all required fields."""
        status = ServerStatus(
            name="test",
            status=ServerStatusEnum.ONLINE,
            tool_count=5,
            resource_count=2,
            prompt_count=1,
            pending_request_count=0,
        )

        assert status.name == "test"
        assert status.status == ServerStatusEnum.ONLINE
        assert status.tool_count == 5
        assert status.resource_count == 2
        assert status.prompt_count == 1
        assert status.pending_request_count == 0

"""Tests for gateway identity detection to prevent recursive spawning."""

import pytest
from dataclasses import dataclass


@dataclass
class MockServerConfig:
    """Mock server config for testing."""

    name: str
    command: str
    args: list[str]


# Import after defining mock to avoid circular import issues
from pmcp.identity import is_self_reference, filter_self_references


class TestIsSelfReference:
    """Tests for is_self_reference detection."""

    def test_direct_pmcp_command(self):
        """Direct 'pmcp' command should be detected."""
        config = MockServerConfig(name="my-gateway", command="pmcp", args=[])
        assert is_self_reference(config) is True

    def test_direct_mcp_gateway_command(self):
        """Direct 'mcp-gateway' command should be detected."""
        config = MockServerConfig(name="gateway", command="mcp-gateway", args=[])
        assert is_self_reference(config) is True

    def test_uvx_pmcp(self):
        """uvx pmcp should be detected."""
        config = MockServerConfig(name="pmcp", command="uvx", args=["pmcp"])
        assert is_self_reference(config) is True

    def test_uvx_pmcp_with_extra_args(self):
        """uvx pmcp with extra args should be detected."""
        config = MockServerConfig(
            name="gateway", command="uvx", args=["pmcp", "--cache-dir", "/tmp"]
        )
        assert is_self_reference(config) is True

    def test_pipx_run_pmcp(self):
        """pipx run pmcp should be detected."""
        config = MockServerConfig(name="gateway", command="pipx", args=["run", "pmcp"])
        assert is_self_reference(config) is True

    def test_python_m_pmcp(self):
        """python -m pmcp should be detected."""
        config = MockServerConfig(name="gateway", command="python", args=["-m", "pmcp"])
        assert is_self_reference(config) is True

    def test_python3_m_pmcp(self):
        """python3 -m pmcp should be detected."""
        config = MockServerConfig(
            name="gateway", command="python3", args=["-m", "pmcp"]
        )
        assert is_self_reference(config) is True

    def test_full_path_pmcp(self):
        """Full path to pmcp should be detected."""
        config = MockServerConfig(
            name="gateway", command="/usr/local/bin/pmcp", args=[]
        )
        assert is_self_reference(config) is True

    def test_uvx_full_path(self):
        """uvx with full path should be detected."""
        config = MockServerConfig(
            name="gateway", command="uvx", args=["/home/user/.local/bin/pmcp"]
        )
        assert is_self_reference(config) is True

    def test_name_mcp_gateway(self):
        """Config named 'mcp-gateway' should be detected (legacy)."""
        config = MockServerConfig(
            name="mcp-gateway", command="some-other-command", args=[]
        )
        assert is_self_reference(config) is True

    def test_name_pmcp(self):
        """Config named 'pmcp' should be detected."""
        config = MockServerConfig(name="pmcp", command="something-else", args=[])
        assert is_self_reference(config) is True

    def test_filesystem_server_not_detected(self):
        """Regular filesystem server should not be detected."""
        config = MockServerConfig(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        )
        assert is_self_reference(config) is False

    def test_playwright_server_not_detected(self):
        """Playwright server should not be detected."""
        config = MockServerConfig(
            name="playwright",
            command="npx",
            args=["-y", "@anthropics/mcp-server-playwright"],
        )
        assert is_self_reference(config) is False

    def test_memory_server_not_detected(self):
        """Memory server should not be detected."""
        config = MockServerConfig(
            name="memory",
            command="npx",
            args=["-y", "@anthropics/mcp-server-memory"],
        )
        assert is_self_reference(config) is False

    def test_uvx_other_package(self):
        """uvx with other package should not be detected."""
        config = MockServerConfig(
            name="some-tool", command="uvx", args=["some-other-mcp-server"]
        )
        assert is_self_reference(config) is False

    def test_case_insensitive_command(self):
        """Detection should be case insensitive."""
        config = MockServerConfig(name="gateway", command="PMCP", args=[])
        assert is_self_reference(config) is True

    def test_case_insensitive_args(self):
        """Detection should be case insensitive in args."""
        config = MockServerConfig(name="gateway", command="uvx", args=["PMCP"])
        assert is_self_reference(config) is True


class TestFilterSelfReferences:
    """Tests for filter_self_references function."""

    def test_filters_out_gateway(self):
        """Should filter out gateway configs."""
        configs = [
            MockServerConfig(name="pmcp", command="uvx", args=["pmcp"]),
            MockServerConfig(
                name="filesystem",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            ),
            MockServerConfig(
                name="memory", command="npx", args=["-y", "@anthropics/mcp-server-memory"]
            ),
        ]
        filtered = filter_self_references(configs)
        assert len(filtered) == 2
        assert all(c.name != "pmcp" for c in filtered)

    def test_preserves_order(self):
        """Should preserve order of non-gateway configs."""
        configs = [
            MockServerConfig(name="first", command="npx", args=["first-server"]),
            MockServerConfig(name="pmcp", command="uvx", args=["pmcp"]),
            MockServerConfig(name="second", command="npx", args=["second-server"]),
            MockServerConfig(name="third", command="npx", args=["third-server"]),
        ]
        filtered = filter_self_references(configs)
        assert len(filtered) == 3
        assert filtered[0].name == "first"
        assert filtered[1].name == "second"
        assert filtered[2].name == "third"

    def test_empty_list(self):
        """Should handle empty list."""
        filtered = filter_self_references([])
        assert filtered == []

    def test_all_gateways(self):
        """Should return empty list if all configs are gateways."""
        configs = [
            MockServerConfig(name="pmcp", command="pmcp", args=[]),
            MockServerConfig(name="gateway", command="uvx", args=["pmcp"]),
            MockServerConfig(name="mcp-gateway", command="mcp-gateway", args=[]),
        ]
        filtered = filter_self_references(configs)
        assert filtered == []

    def test_no_gateways(self):
        """Should return all configs if none are gateways."""
        configs = [
            MockServerConfig(name="filesystem", command="npx", args=["fs-server"]),
            MockServerConfig(name="memory", command="npx", args=["memory-server"]),
        ]
        filtered = filter_self_references(configs)
        assert len(filtered) == 2


class TestRecursiveSpawnScenario:
    """Test the exact scenario that caused the fork bomb."""

    def test_user_global_config_scenario(self):
        """Simulate the ~/.mcp.json config that caused the fork bomb.

        The user had:
        {
            "mcpServers": {
                "pmcp": { "command": "uvx", "args": ["pmcp"] },
                "filesystem": { "command": "npx", "args": [...] }
            }
        }

        The old filter `c.name != "mcp-gateway"` would have passed "pmcp" through.
        The new filter should catch it.
        """
        configs = [
            MockServerConfig(name="pmcp", command="uvx", args=["pmcp"]),
            MockServerConfig(
                name="filesystem",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            ),
        ]

        # This is what the old filter did - WRONG
        old_filter = [c for c in configs if c.name != "mcp-gateway"]
        assert len(old_filter) == 2  # Old filter passed "pmcp" through!
        assert any(c.name == "pmcp" for c in old_filter)  # BUG!

        # This is what the new filter does - CORRECT
        new_filter = filter_self_references(configs)
        assert len(new_filter) == 1  # New filter catches "pmcp"
        assert all(c.name != "pmcp" for c in new_filter)  # Fixed!
        assert new_filter[0].name == "filesystem"

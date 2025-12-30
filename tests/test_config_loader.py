"""Tests for config loader."""

from __future__ import annotations

import json
from pathlib import Path


from pmcp.config.loader import (
    load_configs,
    make_tool_id,
    parse_tool_id,
)


class TestMakeToolId:
    """Tests for make_tool_id."""

    def test_creates_tool_id(self) -> None:
        assert make_tool_id("github", "create_issue") == "github::create_issue"
        assert make_tool_id("my-server", "my-tool") == "my-server::my-tool"


class TestParseToolId:
    """Tests for parse_tool_id."""

    def test_parses_valid_tool_ids(self) -> None:
        result = parse_tool_id("github::create_issue")
        assert result == ("github", "create_issue")

    def test_returns_none_for_invalid(self) -> None:
        assert parse_tool_id("invalid") is None
        assert parse_tool_id("too::many::parts") is None
        assert parse_tool_id("") is None


class TestLoadConfigs:
    """Tests for load_configs."""

    def test_loads_project_config(self, tmp_path: Path) -> None:
        # Create project config
        project_config = {
            "mcpServers": {
                "test-server": {
                    "command": "node",
                    "args": ["server.js"],
                }
            }
        }
        (tmp_path / ".mcp.json").write_text(json.dumps(project_config))

        configs = load_configs(
            project_root=tmp_path,
            user_config_paths=[],  # No user configs
        )

        assert len(configs) == 1
        assert configs[0].name == "test-server"
        assert configs[0].source == "project"
        assert configs[0].config.command == "node"
        assert configs[0].config.args == ["server.js"]

    def test_merges_configs_with_precedence(self, tmp_path: Path) -> None:
        # Create project config
        project_config = {
            "mcpServers": {
                "shared-server": {"command": "project-cmd"},
                "project-only": {"command": "project-only-cmd"},
            }
        }
        (tmp_path / ".mcp.json").write_text(json.dumps(project_config))

        # Create user config
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        user_config = {
            "mcpServers": {
                "shared-server": {"command": "user-cmd"},  # Should be overridden
                "user-only": {"command": "user-only-cmd"},
            }
        }
        user_config_path = user_dir / "user.mcp.json"
        user_config_path.write_text(json.dumps(user_config))

        configs = load_configs(
            project_root=tmp_path,
            user_config_paths=[user_config_path],
        )

        assert len(configs) == 3

        # Project 'shared-server' should take precedence
        shared = next(c for c in configs if c.name == "shared-server")
        assert shared.source == "project"
        assert shared.config.command == "project-cmd"

        # Both unique servers should be present
        assert any(c.name == "project-only" for c in configs)
        assert any(c.name == "user-only" for c in configs)

    def test_handles_missing_files(self, tmp_path: Path) -> None:
        configs = load_configs(
            project_root=tmp_path / "nonexistent",
            user_config_paths=[tmp_path / "nonexistent.json"],
        )
        assert len(configs) == 0

    def test_handles_invalid_json(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text("invalid json {{{")

        configs = load_configs(
            project_root=tmp_path,
            user_config_paths=[],
        )
        assert len(configs) == 0

    def test_normalizes_relative_paths(self, tmp_path: Path) -> None:
        project_config = {
            "mcpServers": {
                "test-server": {
                    "command": "./bin/server",
                    "cwd": "./data",
                }
            }
        }
        (tmp_path / ".mcp.json").write_text(json.dumps(project_config))

        configs = load_configs(
            project_root=tmp_path,
            user_config_paths=[],
        )

        assert configs[0].config.command == str(tmp_path / "bin" / "server")
        assert configs[0].config.cwd == str(tmp_path / "data")

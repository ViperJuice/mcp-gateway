"""Tests for CLI module."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_gateway.cli import parse_args, setup_logging


class TestParseArgs:
    """Tests for argument parsing."""

    def test_default_args(self) -> None:
        """Test default argument values."""
        with patch("sys.argv", ["mcp-gateway"]):
            args = parse_args()

        assert args.command is None
        assert args.project is None
        assert args.config is None
        assert args.policy is None
        assert args.log_level == "info"
        assert args.debug is False
        assert args.quiet is False

    def test_debug_flag(self) -> None:
        """Test debug flag parsing."""
        with patch("sys.argv", ["mcp-gateway", "--debug"]):
            args = parse_args()
        assert args.debug is True

    def test_quiet_flag(self) -> None:
        """Test quiet flag parsing."""
        with patch("sys.argv", ["mcp-gateway", "-q"]):
            args = parse_args()
        assert args.quiet is True

    def test_log_level(self) -> None:
        """Test log level argument."""
        with patch("sys.argv", ["mcp-gateway", "-l", "debug"]):
            args = parse_args()
        assert args.log_level == "debug"

    def test_project_path(self, tmp_path: Path) -> None:
        """Test project path argument."""
        with patch("sys.argv", ["mcp-gateway", "--project", str(tmp_path)]):
            args = parse_args()
        assert args.project == tmp_path

    def test_config_path(self, tmp_path: Path) -> None:
        """Test config path argument."""
        config_file = tmp_path / "config.json"
        config_file.touch()

        with patch("sys.argv", ["mcp-gateway", "--config", str(config_file)]):
            args = parse_args()
        assert args.config == config_file

    def test_policy_path(self, tmp_path: Path) -> None:
        """Test policy path argument."""
        policy_file = tmp_path / "policy.yaml"

        with patch("sys.argv", ["mcp-gateway", "--policy", str(policy_file)]):
            args = parse_args()
        assert args.policy == policy_file

    def test_refresh_command(self) -> None:
        """Test refresh subcommand."""
        with patch("sys.argv", ["mcp-gateway", "refresh"]):
            args = parse_args()
        assert args.command == "refresh"

    def test_refresh_with_server(self) -> None:
        """Test refresh with specific server."""
        with patch("sys.argv", ["mcp-gateway", "refresh", "--server", "github"]):
            args = parse_args()
        assert args.command == "refresh"
        assert args.server == "github"

    def test_refresh_with_force(self) -> None:
        """Test refresh with force flag."""
        with patch("sys.argv", ["mcp-gateway", "refresh", "--force"]):
            args = parse_args()
        assert args.command == "refresh"
        assert args.force is True


class TestSetupLogging:
    """Tests for logging setup."""

    def test_info_logging_level(self) -> None:
        """Test info logging level maps correctly."""
        # Reset logging handlers for clean test
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        root.setLevel(logging.NOTSET)

        setup_logging("info")
        assert root.level == logging.INFO

    def test_debug_logging_level(self) -> None:
        """Test debug logging level maps correctly."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        root.setLevel(logging.NOTSET)

        setup_logging("debug")
        assert root.level == logging.DEBUG

    def test_error_logging_level(self) -> None:
        """Test error logging level maps correctly."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        root.setLevel(logging.NOTSET)

        setup_logging("error")
        assert root.level == logging.ERROR


class TestMain:
    """Tests for main entry point."""

    def test_main_loads_dotenv(self) -> None:
        """Test that main loads .env file."""
        from mcp_gateway.cli import main

        with patch("mcp_gateway.cli.load_dotenv") as mock_dotenv:
            with patch("mcp_gateway.cli.parse_args") as mock_parse:
                mock_parse.return_value = argparse.Namespace(
                    command=None,
                    project=None,
                    config=None,
                    policy=None,
                    log_level="info",
                    debug=False,
                    quiet=False,
                )

                with patch("asyncio.run") as mock_run:
                    mock_run.side_effect = KeyboardInterrupt()

                    main()

            mock_dotenv.assert_called_once()

    def test_main_handles_keyboard_interrupt(self) -> None:
        """Test that main handles KeyboardInterrupt gracefully."""
        from mcp_gateway.cli import main

        with patch("mcp_gateway.cli.load_dotenv"):
            with patch("mcp_gateway.cli.parse_args") as mock_parse:
                mock_parse.return_value = argparse.Namespace(
                    command=None,
                    project=None,
                    config=None,
                    policy=None,
                    log_level="info",
                    debug=False,
                    quiet=False,
                )

                with patch("asyncio.run") as mock_run:
                    mock_run.side_effect = KeyboardInterrupt()

                    # Should not raise
                    main()

    def test_main_exits_on_error(self) -> None:
        """Test that main exits with code 1 on error."""
        from mcp_gateway.cli import main

        with patch("mcp_gateway.cli.load_dotenv"):
            with patch("mcp_gateway.cli.parse_args") as mock_parse:
                mock_parse.return_value = argparse.Namespace(
                    command=None,
                    project=None,
                    config=None,
                    policy=None,
                    log_level="info",
                    debug=False,
                    quiet=False,
                )

                with patch("asyncio.run") as mock_run:
                    mock_run.side_effect = RuntimeError("Fatal error")

                    with pytest.raises(SystemExit) as exc_info:
                        main()

                    assert exc_info.value.code == 1

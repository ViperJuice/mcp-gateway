"""Tests for CLI module."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pmcp.cli import parse_args, setup_logging


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

    def test_status_command(self) -> None:
        """Test status subcommand."""
        with patch("sys.argv", ["mcp-gateway", "status"]):
            args = parse_args()
        assert args.command == "status"

    def test_status_with_json(self) -> None:
        """Test status with json flag."""
        with patch("sys.argv", ["mcp-gateway", "status", "--json"]):
            args = parse_args()
        assert args.command == "status"
        assert args.json is True

    def test_status_with_server_filter(self) -> None:
        """Test status with specific server filter."""
        with patch("sys.argv", ["mcp-gateway", "status", "--server", "playwright"]):
            args = parse_args()
        assert args.command == "status"
        assert args.server == "playwright"

    def test_status_with_pending(self) -> None:
        """Test status with pending flag."""
        with patch("sys.argv", ["mcp-gateway", "status", "--pending"]):
            args = parse_args()
        assert args.command == "status"
        assert args.pending is True

    def test_status_with_verbose(self) -> None:
        """Test status with verbose flag."""
        with patch("sys.argv", ["mcp-gateway", "status", "-v"]):
            args = parse_args()
        assert args.command == "status"
        assert args.verbose is True

    def test_status_all_options(self) -> None:
        """Test status with all options combined."""
        with patch(
            "sys.argv",
            [
                "mcp-gateway",
                "status",
                "--json",
                "--server",
                "github",
                "--pending",
                "-v",
            ],
        ):
            args = parse_args()
        assert args.command == "status"
        assert args.json is True
        assert args.server == "github"
        assert args.pending is True
        assert args.verbose is True


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
        from pmcp.cli import main

        with patch("pmcp.cli.load_dotenv") as mock_dotenv:
            with patch("pmcp.cli.parse_args") as mock_parse:
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
        from pmcp.cli import main

        with patch("pmcp.cli.load_dotenv"):
            with patch("pmcp.cli.parse_args") as mock_parse:
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
        from pmcp.cli import main

        with patch("pmcp.cli.load_dotenv"):
            with patch("pmcp.cli.parse_args") as mock_parse:
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


class TestRunStatus:
    """Tests for run_status function."""

    @pytest.fixture
    def status_args(self) -> argparse.Namespace:
        """Create default status args."""
        return argparse.Namespace(
            command="status",
            json=False,
            server=None,
            pending=False,
            verbose=False,
            project=None,
            config=None,
            policy=None,
            log_level="warn",
        )

    @pytest.mark.asyncio
    async def test_status_no_servers(
        self, status_args: argparse.Namespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test status output when no servers configured."""
        from pmcp.cli import run_status

        with patch("pmcp.config.loader.load_configs", return_value=[]):
            await run_status(status_args)

        captured = capsys.readouterr()
        assert "No MCP servers configured" in captured.out

    @pytest.mark.asyncio
    async def test_status_json_no_servers(
        self, status_args: argparse.Namespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test JSON output when no servers configured."""
        import json

        from pmcp.cli import run_status

        status_args.json = True

        with patch("pmcp.config.loader.load_configs", return_value=[]):
            await run_status(status_args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["servers"] == []
        assert output["tools"] == 0


class TestLogsCommand:
    """Tests for logs command parsing."""

    def test_logs_command(self) -> None:
        """Test logs subcommand."""
        with patch("sys.argv", ["mcp-gateway", "logs"]):
            args = parse_args()
        assert args.command == "logs"

    def test_logs_with_follow(self) -> None:
        """Test logs with follow flag."""
        with patch("sys.argv", ["mcp-gateway", "logs", "--follow"]):
            args = parse_args()
        assert args.command == "logs"
        assert args.follow is True

    def test_logs_with_tail(self) -> None:
        """Test logs with tail option."""
        with patch("sys.argv", ["mcp-gateway", "logs", "--tail", "100"]):
            args = parse_args()
        assert args.command == "logs"
        assert args.tail == 100

    def test_logs_with_level(self) -> None:
        """Test logs with level filter."""
        with patch("sys.argv", ["mcp-gateway", "logs", "--level", "error"]):
            args = parse_args()
        assert args.command == "logs"
        assert args.level == "error"

    def test_logs_with_server(self) -> None:
        """Test logs with server filter."""
        with patch("sys.argv", ["mcp-gateway", "logs", "--server", "github"]):
            args = parse_args()
        assert args.command == "logs"
        assert args.server == "github"


class TestInitCommand:
    """Tests for init command parsing."""

    def test_init_command(self) -> None:
        """Test init subcommand."""
        with patch("sys.argv", ["mcp-gateway", "init"]):
            args = parse_args()
        assert args.command == "init"

    def test_init_with_project(self, tmp_path: Path) -> None:
        """Test init with project directory."""
        with patch("sys.argv", ["mcp-gateway", "init", "--project", str(tmp_path)]):
            args = parse_args()
        assert args.command == "init"
        assert args.project == tmp_path

    def test_init_with_force(self) -> None:
        """Test init with force flag."""
        with patch("sys.argv", ["mcp-gateway", "init", "--force"]):
            args = parse_args()
        assert args.command == "init"
        assert args.force is True


class TestRunLogs:
    """Tests for run_logs function."""

    @pytest.mark.asyncio
    async def test_logs_no_file(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test logs output when no log file exists."""
        from pmcp.cli import run_logs

        # Create args
        args = argparse.Namespace(
            command="logs",
            follow=False,
            tail=50,
            level=None,
            server=None,
        )

        # Patch LOG_FILE to non-existent path
        with patch("pmcp.cli.LOG_FILE", tmp_path / "nonexistent.log"):
            await run_logs(args)

        captured = capsys.readouterr()
        assert "No log file found" in captured.out

    @pytest.mark.asyncio
    async def test_logs_reads_file(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test logs reads existing log file."""
        from pmcp.cli import run_logs

        # Create test log file
        log_file = tmp_path / "test.log"
        log_file.write_text(
            "[2024-01-01T00:00:00] [INFO] Test message 1\n"
            "[2024-01-01T00:00:01] [INFO] Test message 2\n"
        )

        args = argparse.Namespace(
            command="logs",
            follow=False,
            tail=50,
            level=None,
            server=None,
        )

        with patch("pmcp.cli.LOG_FILE", log_file):
            await run_logs(args)

        captured = capsys.readouterr()
        assert "Test message 1" in captured.out
        assert "Test message 2" in captured.out

    @pytest.mark.asyncio
    async def test_logs_with_level_filter(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test logs filters by level."""
        from pmcp.cli import run_logs

        log_file = tmp_path / "test.log"
        log_file.write_text(
            "[2024-01-01T00:00:00] [INFO] Info message\n"
            "[2024-01-01T00:00:01] [ERROR] Error message\n"
            "[2024-01-01T00:00:02] [DEBUG] Debug message\n"
        )

        args = argparse.Namespace(
            command="logs",
            follow=False,
            tail=50,
            level="error",
            server=None,
        )

        with patch("pmcp.cli.LOG_FILE", log_file):
            await run_logs(args)

        captured = capsys.readouterr()
        assert "Error message" in captured.out
        assert "Info message" not in captured.out

    @pytest.mark.asyncio
    async def test_logs_with_server_filter(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test logs filters by server name."""
        from pmcp.cli import run_logs

        log_file = tmp_path / "test.log"
        log_file.write_text(
            "[2024-01-01T00:00:00] [INFO] [github] GitHub message\n"
            "[2024-01-01T00:00:01] [INFO] [playwright] Playwright message\n"
        )

        args = argparse.Namespace(
            command="logs",
            follow=False,
            tail=50,
            level=None,
            server="github",
        )

        with patch("pmcp.cli.LOG_FILE", log_file):
            await run_logs(args)

        captured = capsys.readouterr()
        assert "GitHub message" in captured.out
        assert "Playwright message" not in captured.out

    @pytest.mark.asyncio
    async def test_logs_with_tail_limit(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test logs respects tail limit."""
        from pmcp.cli import run_logs

        log_file = tmp_path / "test.log"
        lines = [f"[2024-01-01T00:00:{i:02d}] [INFO] Line {i}\n" for i in range(10)]
        log_file.write_text("".join(lines))

        args = argparse.Namespace(
            command="logs",
            follow=False,
            tail=3,
            level=None,
            server=None,
        )

        with patch("pmcp.cli.LOG_FILE", log_file):
            await run_logs(args)

        captured = capsys.readouterr()
        # Should only show last 3 lines
        assert "Line 7" in captured.out
        assert "Line 8" in captured.out
        assert "Line 9" in captured.out
        assert "Line 0" not in captured.out


class TestRunInit:
    """Tests for run_init function."""

    @pytest.mark.asyncio
    async def test_init_creates_config(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test init creates .mcp.json file."""
        from pmcp.cli import run_init

        args = argparse.Namespace(
            command="init",
            project=tmp_path,
            force=False,
        )

        # Mock user input to select no servers
        with patch("builtins.input", return_value=""):
            await run_init(args)

        config_file = tmp_path / ".mcp.json"
        assert config_file.exists()

    @pytest.mark.asyncio
    async def test_init_aborts_if_exists(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test init aborts if config already exists."""
        from pmcp.cli import run_init

        # Create existing config
        config_file = tmp_path / ".mcp.json"
        config_file.write_text('{"mcpServers": {}}')

        args = argparse.Namespace(
            command="init",
            project=tmp_path,
            force=False,
        )

        await run_init(args)

        captured = capsys.readouterr()
        assert "already exists" in captured.out

    @pytest.mark.asyncio
    async def test_init_force_overwrites(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """Test init --force overwrites existing config."""
        from pmcp.cli import run_init

        # Create existing config
        config_file = tmp_path / ".mcp.json"
        config_file.write_text('{"mcpServers": {"old": {}}}')

        args = argparse.Namespace(
            command="init",
            project=tmp_path,
            force=True,
        )

        with patch("builtins.input", return_value=""):
            await run_init(args)

        # Config should be overwritten
        content = config_file.read_text()
        assert "old" not in content


class TestRunStatusWithData:
    """Tests for run_status with actual server data."""

    @pytest.fixture
    def mock_server_status(self) -> MagicMock:
        """Create mock server status."""
        from pmcp.types import ServerStatus, ServerStatusEnum

        return ServerStatus(
            name="test-server",
            status=ServerStatusEnum.ONLINE,
            tool_count=5,
            resource_count=2,
            prompt_count=1,
        )

    @pytest.mark.asyncio
    async def test_status_shows_servers(
        self,
        capsys: pytest.CaptureFixture[str],
        mock_server_status: MagicMock,
    ) -> None:
        """Test status shows server information."""
        from pmcp.cli import run_status

        args = argparse.Namespace(
            command="status",
            json=False,
            server=None,
            pending=False,
            verbose=False,
            project=None,
            config=None,
            policy=None,
            log_level="warn",
        )

        with patch("pmcp.config.loader.load_configs", return_value=[]):
            with patch(
                "pmcp.client.ClientManager.get_all_server_statuses",
                return_value=[mock_server_status],
            ):
                with patch(
                    "pmcp.client.ClientManager.connect_all",
                    return_value=[],
                ):
                    await run_status(args)

        captured = capsys.readouterr()
        assert "test-server" in captured.out or "No MCP servers" in captured.out

    @pytest.mark.asyncio
    async def test_status_json_with_data(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test status JSON output includes server data."""
        import json

        from pmcp.cli import run_status

        args = argparse.Namespace(
            command="status",
            json=True,
            server=None,
            pending=False,
            verbose=False,
            project=None,
            config=None,
            policy=None,
            log_level="warn",
        )

        with patch("pmcp.config.loader.load_configs", return_value=[]):
            await run_status(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "servers" in output
        assert "tools" in output

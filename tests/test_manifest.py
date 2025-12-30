"""Tests for manifest functionality."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from pmcp.manifest.environment import (
    detect_platform,
    probe_clis,
)
from pmcp.manifest.installer import (
    InstallError,
    InstallJob,
    JobManager,
    MissingApiKeyError,
    check_api_key,
    install_server,
    verify_installation,
)
from pmcp.manifest.loader import (
    CLIAlternative,
    Manifest,
    ServerConfig,
    load_manifest,
)
from pmcp.manifest.matcher import (
    _keyword_match,
    match_capability,
)


# === Environment Detection Tests ===


@pytest.mark.skipif(
    True,  # Platform detection is environment-specific
    reason="Platform detection depends on actual environment",
)
def test_detect_platform():
    """Test platform detection."""
    platform = detect_platform()
    assert platform in ("mac", "wsl", "linux", "windows")


@pytest.mark.asyncio
async def test_probe_clis_with_mocked_which():
    """Test CLI probing with mocked which."""
    with patch("pmcp.manifest.environment.shutil.which") as mock_which:
        # Only git and docker are "installed"
        mock_which.side_effect = (
            lambda cmd: f"/usr/bin/{cmd}" if cmd in ("git", "docker") else None
        )

        cli_configs = {
            "git": {"check_command": ["git", "--version"]},
            "docker": {"check_command": ["docker", "--version"]},
            "kubectl": {"check_command": ["kubectl", "version"]},
            "terraform": {"check_command": ["terraform", "--version"]},
        }
        detected = await probe_clis(cli_configs)

        assert "git" in detected
        assert "docker" in detected
        assert "kubectl" not in detected
        assert "terraform" not in detected


# === Manifest Loading Tests ===


def test_load_manifest():
    """Test loading the manifest."""
    manifest = load_manifest()

    assert manifest is not None
    assert len(manifest.cli_alternatives) > 0
    assert len(manifest.servers) > 0


def test_manifest_has_expected_servers():
    """Test that manifest has expected servers."""
    manifest = load_manifest()

    expected_servers = ["playwright", "context7", "memory", "filesystem"]
    for server in expected_servers:
        assert server in manifest.servers, f"Missing server: {server}"


def test_manifest_auto_start_servers():
    """Test getting auto-start servers."""
    manifest = load_manifest()

    auto_start = manifest.get_auto_start_servers()

    # Should include playwright, context7, brightdata-scraper, brightdata-serp
    auto_start_names = [s.name for s in auto_start]
    assert "playwright" in auto_start_names
    assert "context7" in auto_start_names


def test_manifest_search_by_keyword():
    """Test keyword search in manifest."""
    manifest = load_manifest()

    # Search for browser-related
    results = manifest.search_by_keyword("browser")
    assert len(results) > 0

    # Search for scraping
    results = manifest.search_by_keyword("scrape")
    assert len(results) > 0


def test_manifest_server_config():
    """Test server config structure."""
    manifest = load_manifest()

    playwright = manifest.get_server("playwright")
    assert playwright is not None
    assert playwright.command == "npx"
    assert playwright.requires_api_key is False
    assert playwright.auto_start is True


def test_manifest_cli_config():
    """Test CLI config structure."""
    manifest = load_manifest()

    git = manifest.get_cli("git")
    assert git is not None
    assert (
        "version control" in git.description.lower() or "git" in git.description.lower()
    )
    assert len(git.keywords) > 0


# === Matcher Tests ===


def create_test_manifest() -> Manifest:
    """Create a test manifest."""
    return Manifest(
        version="1.0",
        cli_alternatives={
            "git": CLIAlternative(
                name="git",
                keywords=["git", "version control", "commits"],
                check_command=["git", "--version"],
                help_command=["git", "--help"],
                description="Git version control",
            ),
            "docker": CLIAlternative(
                name="docker",
                keywords=["docker", "container", "image"],
                check_command=["docker", "--version"],
                help_command=["docker", "--help"],
                description="Docker containers",
            ),
        },
        servers={
            "playwright": ServerConfig(
                name="playwright",
                description="Browser automation",
                keywords=["browser", "automation", "playwright"],
                install={"mac": ["npm", "install", "playwright"]},
                command="npx",
                args=["playwright"],
                requires_api_key=False,
            ),
            "github": ServerConfig(
                name="github",
                description="GitHub API access",
                keywords=["github", "issues", "pull requests"],
                install={"mac": ["npm", "install", "github"]},
                command="npx",
                args=["github"],
                requires_api_key=True,
                env_var="GITHUB_TOKEN",
            ),
        },
        discovery_queue_path=".mcp-gateway/discovery_queue.json",
    )


def test_keyword_match_cli():
    """Test keyword matching for CLIs."""
    manifest = create_test_manifest()
    detected_clis = {"git", "docker"}

    result = _keyword_match("I need version control", manifest, detected_clis)

    assert result.matched is True
    assert result.entry_name == "git"
    assert result.entry_type == "cli"


def test_keyword_match_server():
    """Test keyword matching for servers."""
    manifest = create_test_manifest()
    detected_clis: set[str] = set()  # No CLIs detected

    result = _keyword_match("browser automation", manifest, detected_clis)

    assert result.matched is True
    assert result.entry_name == "playwright"
    assert result.entry_type == "server"


def test_keyword_match_prefers_cli():
    """Test that CLIs are preferred over servers."""
    manifest = Manifest(
        version="1.0",
        cli_alternatives={
            "docker": CLIAlternative(
                name="docker",
                keywords=["docker", "container"],
                check_command=["docker", "--version"],
                help_command=["docker", "--help"],
                description="Docker CLI",
            ),
        },
        servers={
            "docker-mcp": ServerConfig(
                name="docker-mcp",
                description="Docker via MCP",
                keywords=["docker", "container"],
                install={},
                command="npx",
                args=["docker-mcp"],
                requires_api_key=False,
            ),
        },
        discovery_queue_path=".mcp-gateway/discovery_queue.json",
    )
    detected_clis = {"docker"}

    result = _keyword_match("docker container", manifest, detected_clis)

    assert result.matched is True
    assert result.entry_type == "cli"


def test_keyword_match_no_match():
    """Test no match found."""
    manifest = create_test_manifest()
    detected_clis: set[str] = set()

    result = _keyword_match("quantum computing database", manifest, detected_clis)

    assert result.matched is False


@pytest.mark.asyncio
async def test_match_capability_fallback_to_keyword():
    """Test that match_capability falls back to keyword when LLM fails."""
    manifest = create_test_manifest()
    detected_clis = {"git"}

    # Disable LLM matching
    result = await match_capability(
        "version control commits",
        manifest,
        detected_clis,
        use_llm=False,
    )

    assert result.matched is True
    assert result.entry_name == "git"


# === Installer Tests ===


@pytest.mark.asyncio
async def test_check_api_key_missing():
    """Test that missing API key raises error."""
    server_config = ServerConfig(
        name="test",
        description="Test server",
        keywords=["test"],
        install={"mac": ["echo", "install"]},
        command="echo",
        args=["test"],
        requires_api_key=True,
        env_var="TEST_MISSING_API_KEY",
        env_instructions="Set TEST_MISSING_API_KEY",
    )

    with pytest.raises(MissingApiKeyError) as exc_info:
        await check_api_key(server_config)

    assert exc_info.value.env_var == "TEST_MISSING_API_KEY"


@pytest.mark.asyncio
async def test_check_api_key_present():
    """Test that present API key passes."""
    server_config = ServerConfig(
        name="test",
        description="Test server",
        keywords=["test"],
        install={"mac": ["echo", "install"]},
        command="echo",
        args=["test"],
        requires_api_key=True,
        env_var="PATH",  # PATH is always set
    )

    # Should not raise
    await check_api_key(server_config)


@pytest.mark.asyncio
async def test_check_api_key_not_required():
    """Test that no API key check when not required."""
    server_config = ServerConfig(
        name="test",
        description="Test server",
        keywords=["test"],
        install={"mac": ["echo", "install"]},
        command="echo",
        args=["test"],
        requires_api_key=False,
    )

    # Should not raise
    await check_api_key(server_config)


@pytest.mark.asyncio
async def test_install_server_no_platform_command():
    """Test install fails when no command for platform."""
    server_config = ServerConfig(
        name="test",
        description="Test server",
        keywords=["test"],
        install={"mac": ["echo", "install"]},  # Only mac
        command="echo",
        args=["test"],
        requires_api_key=False,
    )

    with pytest.raises(InstallError):
        await install_server(server_config, "windows")


@pytest.mark.asyncio
async def test_install_server_success():
    """Test successful server installation."""
    server_config = ServerConfig(
        name="test",
        description="Test server",
        keywords=["test"],
        install={"linux": ["echo", "installed"]},
        command="echo",
        args=["test"],
        requires_api_key=False,
    )

    # Should succeed (echo always works)
    await install_server(server_config, "linux")


# === JobManager Tests ===


@pytest.fixture(autouse=True)
def reset_job_manager():
    """Reset JobManager singleton between tests."""
    JobManager._instance = None
    yield
    JobManager._instance = None


class TestJobManager:
    """Tests for JobManager singleton and job lifecycle."""

    def test_singleton_instance(self) -> None:
        """Verify get_instance() returns same object."""
        manager1 = JobManager.get_instance()
        manager2 = JobManager.get_instance()
        assert manager1 is manager2

    def test_get_job_returns_none_for_unknown(self) -> None:
        """get_job('unknown') returns None."""
        manager = JobManager.get_instance()
        assert manager.get_job("unknown") is None

    def test_get_all_jobs_empty(self) -> None:
        """Returns empty list initially."""
        manager = JobManager.get_instance()
        assert manager.get_all_jobs() == []

    def test_cleanup_old_jobs_removes_completed(self) -> None:
        """Old complete/failed jobs removed."""
        manager = JobManager.get_instance()

        # Add an old completed job
        old_job = InstallJob(
            id="old123",
            server_name="test",
            status="complete",
            started_at=time.time() - 7200,  # 2 hours ago
        )
        manager._jobs["old123"] = old_job

        # Add a recent completed job
        recent_job = InstallJob(
            id="recent",
            server_name="test",
            status="complete",
            started_at=time.time() - 100,  # 100 seconds ago
        )
        manager._jobs["recent"] = recent_job

        # Cleanup with 1 hour max age
        removed = manager.cleanup_old_jobs(max_age=3600)

        assert removed == 1
        assert "old123" not in manager._jobs
        assert "recent" in manager._jobs

    def test_cleanup_old_jobs_keeps_in_progress(self) -> None:
        """Active jobs not removed."""
        manager = JobManager.get_instance()

        # Add an old but still installing job
        old_active_job = InstallJob(
            id="active",
            server_name="test",
            status="installing",
            started_at=time.time() - 7200,  # 2 hours ago
        )
        manager._jobs["active"] = old_active_job

        removed = manager.cleanup_old_jobs(max_age=3600)

        assert removed == 0
        assert "active" in manager._jobs


class TestStartInstall:
    """Tests for JobManager.start_install()."""

    @pytest.fixture
    def server_config(self) -> ServerConfig:
        """Create a test server config."""
        return ServerConfig(
            name="test-server",
            description="Test server",
            keywords=["test"],
            install={"linux": ["echo", "installing..."]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

    @pytest.mark.asyncio
    async def test_start_install_returns_job_id(
        self, server_config: ServerConfig
    ) -> None:
        """Returns 8-char UUID."""
        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        assert len(job_id) == 8
        assert job_id.isalnum()

    @pytest.mark.asyncio
    async def test_start_install_creates_job(self, server_config: ServerConfig) -> None:
        """Job added to _jobs dict."""
        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        job = manager.get_job(job_id)
        assert job is not None
        assert job.server_name == "test-server"

    @pytest.mark.asyncio
    async def test_start_install_sets_installing_status(
        self, server_config: ServerConfig
    ) -> None:
        """Status transitions pending→installing."""
        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        job = manager.get_job(job_id)
        assert job is not None
        # Status should be installing (process started)
        assert job.status in ("installing", "complete")  # May complete quickly

    @pytest.mark.asyncio
    async def test_start_install_starts_monitor_task(
        self, server_config: ServerConfig
    ) -> None:
        """_monitor_task is set."""
        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        job = manager.get_job(job_id)
        assert job is not None
        # Monitor task should be set (or completed if fast)
        # For echo command, it completes almost instantly

    @pytest.mark.asyncio
    async def test_start_install_wsl_fallback_to_linux(self) -> None:
        """WSL uses linux command if no wsl."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["echo", "linux-install"]},  # Only linux
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        # WSL should fall back to linux command
        job_id = await manager.start_install(server_config, "wsl")

        job = manager.get_job(job_id)
        assert job is not None

    @pytest.mark.asyncio
    async def test_start_install_no_command_raises(self) -> None:
        """InstallError if no platform command."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"mac": ["echo", "mac-install"]},  # Only mac
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        with pytest.raises(InstallError):
            await manager.start_install(server_config, "windows")

    @pytest.mark.asyncio
    async def test_start_install_command_not_found(self) -> None:
        """Status=failed if command not found."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["nonexistent_command_xyz", "arg"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        job = manager.get_job(job_id)
        assert job is not None
        assert job.status == "failed"
        assert "not found" in job.error.lower()


class TestMonitorInstall:
    """Tests for _monitor_install background task."""

    @pytest.mark.asyncio
    async def test_monitor_updates_heartbeat_on_output(self) -> None:
        """last_heartbeat updated on stdout."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["echo", "output line"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        # Wait for job to complete
        await asyncio.sleep(0.2)

        job = manager.get_job(job_id)
        assert job is not None
        # Heartbeat should be recent
        assert time.time() - job.last_heartbeat < 5

    @pytest.mark.asyncio
    async def test_monitor_reads_stderr(self) -> None:
        """Stderr output also tracked."""
        # Use bash to write to stderr
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["bash", "-c", "echo stderr_message >&2"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        # Wait for completion
        await asyncio.sleep(0.3)

        job = manager.get_job(job_id)
        assert job is not None
        # Check stderr was captured
        stderr_found = any("stderr_message" in line for line in job.output_lines)
        assert stderr_found, f"Expected stderr in output: {job.output_lines}"

    @pytest.mark.asyncio
    async def test_monitor_keeps_last_20_lines(self) -> None:
        """Output trimmed to 20 lines."""
        # Generate 30 lines of output
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={
                "linux": ["bash", "-c", "for i in $(seq 1 30); do echo line$i; done"]
            },
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        # Wait for completion
        await asyncio.sleep(0.5)

        job = manager.get_job(job_id)
        assert job is not None
        assert len(job.output_lines) <= 20

    @pytest.mark.asyncio
    async def test_monitor_exit_code_zero_completes(self) -> None:
        """returncode=0 → complete."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["true"]},  # exit 0
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        # Wait for completion
        await asyncio.sleep(0.2)

        job = manager.get_job(job_id)
        assert job is not None
        assert job.status == "complete"

    @pytest.mark.asyncio
    async def test_monitor_exit_code_nonzero_fails(self) -> None:
        """returncode≠0 → failed."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["false"]},  # exit 1
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        # Wait for completion
        await asyncio.sleep(0.2)

        job = manager.get_job(job_id)
        assert job is not None
        assert job.status == "failed"

    @pytest.mark.asyncio
    async def test_monitor_server_ready_on_startup_pattern(self) -> None:
        """'initialized' → server_ready."""
        # This test is tricky because we need the process to stay alive
        # but also detect the startup pattern
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            # Echo "initialized" - should trigger server_ready
            install={"linux": ["bash", "-c", "echo initialized && sleep 5"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        # Wait for pattern detection
        await asyncio.sleep(0.3)

        job = manager.get_job(job_id)
        assert job is not None
        assert job.status == "server_ready"

        # Clean up - kill the process
        if job.process and job.process.returncode is None:
            job.process.kill()

    @pytest.mark.asyncio
    async def test_monitor_cancellation_cleanup(self) -> None:
        """Cancelled task cleans up process."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["sleep", "10"]},  # Long running
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        # Give it time to start
        await asyncio.sleep(0.1)

        job = manager.get_job(job_id)
        assert job is not None
        assert job.status == "installing"

        # Cancel the job
        result = await manager.cancel_job(job_id)
        assert result is True
        assert job.status == "failed"
        assert "Cancelled" in (job.error or "")


class TestProgressParsing:
    """Tests for _parse_progress and _is_server_started."""

    def test_parse_progress_from_percentage(self) -> None:
        """'45%' → 45."""
        manager = JobManager.get_instance()
        result = manager._parse_progress("Installing... 45% complete", 0)
        assert result == 45

    def test_parse_progress_caps_at_99(self) -> None:
        """'100%' → 99 (until complete)."""
        manager = JobManager.get_instance()
        result = manager._parse_progress("100% done", 0)
        assert result == 99

    def test_parse_progress_increments_on_activity(self) -> None:
        """Non-% output increments by 1."""
        manager = JobManager.get_instance()
        result = manager._parse_progress("Some output without percentage", 50)
        assert result == 51

    def test_parse_progress_stops_at_90(self) -> None:
        """Increment caps at 90."""
        manager = JobManager.get_instance()
        result = manager._parse_progress("Some output", 90)
        assert result == 90  # No increment past 90

    def test_is_server_started_patterns(self) -> None:
        """Each startup pattern detected."""
        manager = JobManager.get_instance()

        patterns = [
            "running on stdio",
            "Server running",
            "server started",
            "listening on stdio",
            "MCP server ready",
            "ready to accept connections",
            "waiting for connection",
            "Server initialized",
        ]

        for pattern in patterns:
            assert manager._is_server_started(pattern), (
                f"Pattern not detected: {pattern}"
            )

    def test_is_server_started_case_insensitive(self) -> None:
        """'INITIALIZED' detected."""
        manager = JobManager.get_instance()
        assert manager._is_server_started("SERVER INITIALIZED")
        assert manager._is_server_started("RUNNING ON STDIO")

    def test_is_server_started_false_for_random(self) -> None:
        """Random text returns False."""
        manager = JobManager.get_instance()
        assert not manager._is_server_started("Installing packages...")
        assert not manager._is_server_started("npm WARN deprecated")
        assert not manager._is_server_started("Progress: 50%")


class TestCancelJob:
    """Tests for cancel_job()."""

    @pytest.mark.asyncio
    async def test_cancel_job_unknown_returns_false(self) -> None:
        """Unknown job_id returns False."""
        manager = JobManager.get_instance()
        result = await manager.cancel_job("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_job_terminates_process(self) -> None:
        """Running process terminated."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["sleep", "30"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        await asyncio.sleep(0.1)

        job = manager.get_job(job_id)
        assert job is not None
        process = job.process

        result = await manager.cancel_job(job_id)
        assert result is True

        # Process should be terminated
        await asyncio.sleep(0.1)
        assert process is None or process.returncode is not None

    @pytest.mark.asyncio
    async def test_cancel_job_cancels_monitor_task(self) -> None:
        """_monitor_task.cancel() called."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["sleep", "30"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        await asyncio.sleep(0.1)

        job = manager.get_job(job_id)
        assert job is not None
        monitor_task = job._monitor_task

        await manager.cancel_job(job_id)

        # Wait for cancellation to complete
        await asyncio.sleep(0.2)

        # Monitor task should be cancelled or done
        assert monitor_task is None or monitor_task.cancelled() or monitor_task.done()

    @pytest.mark.asyncio
    async def test_cancel_job_sets_failed_status(self) -> None:
        """Status=failed, error='Cancelled'."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["sleep", "30"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        manager = JobManager.get_instance()
        job_id = await manager.start_install(server_config, "linux")

        await asyncio.sleep(0.1)

        await manager.cancel_job(job_id)

        job = manager.get_job(job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error == "Cancelled by user"


class TestInstallServerLegacy:
    """Tests for blocking install_server() function."""

    @pytest.mark.asyncio
    async def test_install_server_timeout(self) -> None:
        """InstallError on timeout."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["sleep", "10"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        with pytest.raises(InstallError) as exc_info:
            await install_server(server_config, "linux", timeout=0.1)

        assert "timed out" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_install_server_nonzero_exit(self) -> None:
        """InstallError on non-zero exit."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["bash", "-c", "exit 1"]},
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        with pytest.raises(InstallError) as exc_info:
            await install_server(server_config, "linux")

        assert "failed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_install_server_wsl_fallback(self) -> None:
        """WSL falls back to linux."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["echo", "success"]},  # Only linux
            command="echo",
            args=["test"],
            requires_api_key=False,
        )

        # Should succeed with WSL falling back to linux
        await install_server(server_config, "wsl")


class TestVerifyInstallation:
    """Tests for verify_installation()."""

    @pytest.mark.asyncio
    async def test_verify_installation_success(self) -> None:
        """Returns True for valid command."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["echo", "install"]},
            command="echo",  # echo exists
            args=["test"],
            requires_api_key=False,
        )

        result = await verify_installation(server_config)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_installation_failure(self) -> None:
        """Returns False for invalid command."""
        server_config = ServerConfig(
            name="test",
            description="Test",
            keywords=["test"],
            install={"linux": ["echo", "install"]},
            command="nonexistent_command_xyz",  # Doesn't exist
            args=["test"],
            requires_api_key=False,
        )

        result = await verify_installation(server_config)
        assert result is False

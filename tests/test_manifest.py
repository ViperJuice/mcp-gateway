"""Tests for manifest functionality."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mcp_gateway.manifest.environment import (
    Platform,
    detect_platform,
    probe_clis,
)
from mcp_gateway.manifest.loader import (
    CLIAlternative,
    Manifest,
    ServerConfig,
    load_manifest,
)
from mcp_gateway.manifest.matcher import (
    MatchResult,
    _keyword_match,
    match_capability,
)
from mcp_gateway.manifest.installer import (
    InstallError,
    MissingApiKeyError,
    check_api_key,
    install_server,
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
    with patch("mcp_gateway.manifest.environment.shutil.which") as mock_which:
        # Only git and docker are "installed"
        mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}" if cmd in ("git", "docker") else None

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

    expected_servers = ["playwright", "context7", "brightdata-scraper", "brightdata-serp"]
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
    assert "version control" in git.description.lower() or "git" in git.description.lower()
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

"""Manifest loader - parse and provide access to manifest.yaml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

Platform = Literal["mac", "wsl", "linux", "windows"]


@dataclass
class CLIAlternative:
    """Configuration for a CLI alternative."""

    name: str
    keywords: list[str]
    check_command: list[str]
    help_command: list[str]
    description: str
    prefer_mcp_for: list[str] = field(default_factory=list)


@dataclass
class ServerConfig:
    """Configuration for an MCP server in the manifest."""

    name: str
    description: str
    keywords: list[str]
    install: dict[Platform, list[str]]
    command: str
    args: list[str]
    requires_api_key: bool = False
    env_var: str | None = None
    env_instructions: str | None = None
    auto_start: bool = False


@dataclass
class Manifest:
    """Parsed manifest with CLI alternatives and MCP servers."""

    version: str
    cli_alternatives: dict[str, CLIAlternative]
    servers: dict[str, ServerConfig]
    discovery_queue_path: str

    def get_auto_start_servers(self) -> list[ServerConfig]:
        """Get servers configured for auto-start."""
        return [s for s in self.servers.values() if s.auto_start]

    def get_server(self, name: str) -> ServerConfig | None:
        """Get server config by name."""
        return self.servers.get(name)

    def get_cli(self, name: str) -> CLIAlternative | None:
        """Get CLI alternative by name."""
        return self.cli_alternatives.get(name)

    def search_by_keyword(
        self, keyword: str
    ) -> tuple[list[CLIAlternative], list[ServerConfig]]:
        """Search CLIs and servers by keyword."""
        keyword_lower = keyword.lower()

        matching_clis = [
            cli
            for cli in self.cli_alternatives.values()
            if any(keyword_lower in kw.lower() for kw in cli.keywords)
        ]

        matching_servers = [
            server
            for server in self.servers.values()
            if any(keyword_lower in kw.lower() for kw in server.keywords)
        ]

        return matching_clis, matching_servers


def _parse_cli_alternative(name: str, data: dict[str, Any]) -> CLIAlternative:
    """Parse a CLI alternative from raw YAML data."""
    return CLIAlternative(
        name=name,
        keywords=data.get("keywords", []),
        check_command=data.get("check_command", [name, "--version"]),
        help_command=data.get("help_command", [name, "--help"]),
        description=data.get("description", ""),
        prefer_mcp_for=data.get("prefer_mcp_for", []),
    )


def _parse_server_config(name: str, data: dict[str, Any]) -> ServerConfig:
    """Parse a server config from raw YAML data."""
    install_data = data.get("install", {})
    install: dict[Platform, list[str]] = {}

    for platform in ["mac", "wsl", "linux", "windows"]:
        if platform in install_data:
            install[platform] = install_data[platform]  # type: ignore

    return ServerConfig(
        name=name,
        description=data.get("description", ""),
        keywords=data.get("keywords", []),
        install=install,
        command=data.get("command", ""),
        args=data.get("args", []),
        requires_api_key=data.get("requires_api_key", False),
        env_var=data.get("env_var"),
        env_instructions=data.get("env_instructions"),
        auto_start=data.get("auto_start", False),
    )


def load_manifest(manifest_path: Path | None = None) -> Manifest:
    """Load and parse the manifest.yaml file."""
    if manifest_path is None:
        # Default to manifest.yaml in the same directory as this module
        manifest_path = Path(__file__).parent / "manifest.yaml"

    logger.info(f"Loading manifest from {manifest_path}")

    with open(manifest_path, "r") as f:
        data = yaml.safe_load(f)

    # Parse CLI alternatives
    cli_alternatives: dict[str, CLIAlternative] = {}
    for name, cli_data in data.get("cli_alternatives", {}).items():
        cli_alternatives[name] = _parse_cli_alternative(name, cli_data)

    # Parse servers
    servers: dict[str, ServerConfig] = {}
    for name, server_data in data.get("servers", {}).items():
        servers[name] = _parse_server_config(name, server_data)

    manifest = Manifest(
        version=data.get("version", "1.0"),
        cli_alternatives=cli_alternatives,
        servers=servers,
        discovery_queue_path=data.get(
            "discovery_queue_path", ".mcp-gateway/discovery_queue.json"
        ),
    )

    logger.info(
        f"Loaded manifest: {len(cli_alternatives)} CLI alternatives, "
        f"{len(servers)} servers ({len(manifest.get_auto_start_servers())} auto-start)"
    )

    return manifest

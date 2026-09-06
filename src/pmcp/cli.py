#!/usr/bin/env python3
"""PMCP CLI."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from pmcp.auth import redact_auth_url, sanitize_auth_diagnostic
from pmcp.cli_commands.doctor import collect_remote_header_diagnostics
from pmcp.cli_commands.install import (
    detect_install_drift,
    detect_install_method,
)
from pmcp.cli_commands.secrets import (
    run_secrets_check,
    run_secrets_set,
    run_secrets_sync,
)
from pmcp.config.loader import (
    get_startup_policy,
    load_configs,
    set_startup_policy,
)
from pmcp.env_store import record_dotenv_keys
from pmcp.manifest.loader import load_manifest
from pmcp.types import StartupPolicyOperation


from logging.handlers import RotatingFileHandler

LOG_DIR = Path(".pmcp/logs")
LOG_FILE = LOG_DIR / "gateway.log"


class _JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON for Datadog/Loki ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
        )


def setup_logging(
    level: str, log_to_file: bool = True, log_format: str = "text"
) -> None:
    """Configure logging with optional file output."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    text_format = "[%(asctime)s] [%(levelname)s] %(message)s"
    date_format = "%Y-%m-%dT%H:%M:%S"

    formatter: logging.Formatter
    if log_format == "json":
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(text_format, datefmt=date_format)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    # Log to stderr to avoid interfering with MCP stdio
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(handler)

    # Also log to file for later viewing with 'pmcp logs'
    if log_to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=1024 * 1024,  # 1MB
                backupCount=5,
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            logging.getLogger().addHandler(file_handler)
        except Exception:
            # If we can't write logs, continue without file logging
            pass


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    examples = """Examples:
  pmcp
  pmcp --transport http --host 127.0.0.1 --port 3344
  pmcp setup --client claude --mode stdio
  pmcp setup --client claude --mode http --write
  pmcp setup --client opencode --mode http --write
  pmcp doctor
  pmcp upgrade
  pmcp secrets set API_TOKEN my-token --scope user
  pmcp secrets sync --from-scope user --to-scope project --overwrite
  pmcp status --json
  pmcp refresh --force

Environment overrides:
  PMCP_CONFIG, PMCP_POLICY, PMCP_LOG_LEVEL,
  PMCP_TRANSPORT, PMCP_HOST, PMCP_PORT, PMCP_LOCK_DIR, PMCP_AUDIT_JSONL
"""

    parser = argparse.ArgumentParser(
        description="PMCP - Progressive MCP: Minimal context bloat with on-demand tool discovery",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples,
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Default: run server (no subcommand needed)
    parser.add_argument(
        "-p",
        "--project",
        type=Path,
        help="Project root directory (for .mcp.json discovery)",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="Custom MCP config file path",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        help="Policy file path (YAML or JSON)",
    )
    parser.add_argument(
        "-l",
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="info",
        help="Log level (default: info)",
    )
    parser.add_argument(
        "--log-format",
        choices=["text", "json"],
        default="text",
        help="Log output format: text (default) or json (for Datadog/Loki)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only show errors",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="HTTP bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3344,
        help="HTTP port (default: 3344)",
    )
    parser.add_argument(
        "--lock-dir",
        type=Path,
        default=None,
        help="Directory for singleton lock file. Default: ~/.pmcp (global per-user lock)",
    )
    parser.add_argument(
        "--audit-jsonl",
        type=Path,
        default=None,
        help="Explicit scoped-advisor JSONL audit sink. Also read from PMCP_AUDIT_JSONL.",
    )
    parser.add_argument(
        "--auth-token",
        type=str,
        default=None,
        help="Bearer token required on HTTP transport (ignored for stdio). "
        "Also read from PMCP_AUTH_TOKEN env var. "
        "Prefer PMCP_AUTH_TOKEN env var; CLI arg is visible in process list.",
    )
    parser.add_argument(
        "--auth-token-file",
        type=Path,
        default=None,
        help="Path to a file containing the bearer token (alternative to --auth-token). "
        "File contents are stripped of leading/trailing whitespace.",
    )
    parser.add_argument(
        "--max-concurrent-spawns",
        type=int,
        default=None,
        help="Max simultaneous child MCP server processes to spawn (default: 8). "
        "Also read from PMCP_MAX_SPAWNS.",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=None,
        help="HTTP rate limit in requests per minute per client IP on /mcp "
        "(default: 0 = unlimited). Also read from PMCP_RATE_LIMIT.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=None,
        help="HTTP request timeout in seconds (default: 60). Also read from PMCP_REQUEST_TIMEOUT.",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["none", "shared-secret", "resource-server"],
        default=None,
        help="HTTP auth mode (default: inferred — shared-secret if a token is set, "
        "else none). Also read from PMCP_AUTH_MODE.",
    )
    parser.add_argument(
        "--oauth-issuer",
        type=str,
        default=None,
        help="OAuth Authorization Server issuer for resource-server mode. "
        "Also read from PMCP_OAUTH_ISSUER.",
    )
    parser.add_argument(
        "--oauth-jwks-url",
        type=str,
        default=None,
        help="Public https JWKS URL for resource-server mode. "
        "Also read from PMCP_OAUTH_JWKS_URL.",
    )
    parser.add_argument(
        "--oauth-audience",
        type=str,
        default=None,
        help="Canonical resource audience (RFC 8707) for resource-server mode. "
        "Also read from PMCP_OAUTH_AUDIENCE.",
    )
    parser.add_argument(
        "--required-scope",
        action="append",
        default=None,
        dest="required_scopes",
        metavar="SCOPE",
        help="Required OAuth scope for resource-server mode (repeatable). "
        "Also read from PMCP_REQUIRED_SCOPES (comma-separated).",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=None,
        dest="allowed_origins",
        metavar="ORIGIN",
        help="Allowed browser Origin for /mcp (repeatable). Also enables Host "
        "header validation. Also read from PMCP_ALLOWED_ORIGINS (comma-separated).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pmcp {importlib.metadata.version('pmcp')}",
    )

    # Refresh command
    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Refresh capability descriptions for MCP servers",
        description="Pre-generate L1/L2 descriptions for MCP servers. "
        "This avoids LLM calls on every startup.",
    )
    refresh_parser.add_argument(
        "--server",
        "-s",
        type=str,
        help="Refresh only this server (default: all)",
    )
    refresh_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force refresh even if not stale",
    )
    refresh_parser.add_argument(
        "--check-versions",
        action="store_true",
        help="Check for package version updates",
    )
    refresh_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".pmcp"),
        help="Cache directory (default: .pmcp)",
    )
    refresh_parser.add_argument(
        "-l",
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="info",
        help="Log level (default: info)",
    )

    # Update command
    update_parser = subparsers.add_parser(
        "update",
        help="Update subordinate MCP server packages",
        description="Update one or more MCP servers to latest package version via gateway.update_server.",
    )
    update_parser.add_argument(
        "server",
        nargs="?",
        type=str,
        help="Server name to update",
    )
    update_parser.add_argument(
        "--all",
        action="store_true",
        help="Update all servers reported by gateway.health",
    )
    update_parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    update_parser.add_argument(
        "-l",
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="info",
        help="Log level (default: info)",
    )

    # Status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show gateway and server status",
        description="Display status of connected MCP servers and pending requests.",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    status_parser.add_argument(
        "--server",
        "-s",
        type=str,
        help="Show only specific server",
    )
    status_parser.add_argument(
        "--pending",
        action="store_true",
        help="Show pending requests",
    )
    status_parser.add_argument(
        "--probe",
        action="store_true",
        help="Actively connect to each configured server (default: lazy, "
        "report servers without connecting)",
    )
    status_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed information",
    )
    status_parser.add_argument(
        "-p",
        "--project",
        type=Path,
        help="Project root directory (for .mcp.json discovery)",
    )
    status_parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="Custom MCP config file path",
    )
    status_parser.add_argument(
        "--policy",
        type=Path,
        help="Policy file path (YAML or JSON)",
    )
    status_parser.add_argument(
        "-l",
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="warn",  # Default to warn for status command
        help="Log level (default: warn)",
    )

    # Logs command
    logs_parser = subparsers.add_parser(
        "logs",
        help="View gateway logs",
        description="View recent gateway log output.",
    )
    logs_parser.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="Follow log output (like tail -f)",
    )
    logs_parser.add_argument(
        "--tail",
        "-n",
        type=int,
        default=50,
        help="Number of lines to show (default: 50)",
    )
    logs_parser.add_argument(
        "--level",
        choices=["debug", "info", "warn", "error"],
        help="Filter by log level",
    )
    logs_parser.add_argument(
        "--server",
        "-s",
        type=str,
        help="Filter by server name",
    )

    # Init command
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize PMCP configuration",
        description="Create a .mcp.json configuration file interactively.",
    )
    init_parser.add_argument(
        "--project",
        "-p",
        type=Path,
        help="Project directory (default: current directory)",
    )
    init_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing configuration",
    )

    capabilities_parser = subparsers.add_parser(
        "capabilities",
        help="Print machine-readable PMCP capability/version metadata",
    )
    capabilities_parser.add_argument("--json", action="store_true")

    # Setup command
    setup_parser = subparsers.add_parser(
        "setup",
        help="Render PMCP client config",
        description=(
            "Render PMCP config for Claude/OpenCode; optionally write it. "
            "This config starts only the PMCP gateway; downstream servers remain "
            "lazy unless explicitly listed in .mcp.json autoStart."
        ),
    )
    setup_parser.add_argument(
        "--mode",
        choices=["stdio", "sse", "http"],
        default="http",
        help="Connection mode to configure (default: http)",
    )
    setup_parser.add_argument(
        "--client",
        choices=["claude", "opencode"],
        default="claude",
        help="Client config format to render (default: claude)",
    )
    setup_parser.add_argument(
        "--write",
        action="store_true",
        help="Write merged config to the client config file",
    )
    setup_parser.add_argument(
        "--profile",
        choices=[
            "local-stdio",
            "shared-local-http",
            "authenticated-shared-http",
            "ci",
        ],
        help="Named setup profile; preserves --client/--mode defaults when omitted",
    )

    # Config administration command
    config_parser = subparsers.add_parser(
        "config",
        help="Inspect and edit PMCP configuration policy",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_status_parser = config_subparsers.add_parser(
        "status", help="Show effective local configuration status"
    )
    config_status_parser.add_argument("--json", action="store_true")
    config_policy_parser = config_subparsers.add_parser(
        "startup-policy", help="Show persisted startup policy"
    )
    config_policy_parser.add_argument("--json", action="store_true")
    config_set_parser = config_subparsers.add_parser(
        "set-startup-policy", help="Preview or apply an autoStart mutation"
    )
    config_set_parser.add_argument("operation", choices=["add", "remove", "set"])
    config_set_parser.add_argument("names", nargs="*")
    config_set_parser.add_argument("--source", choices=["project", "user", "custom"])
    config_set_parser.add_argument("--path")
    config_set_parser.add_argument("--apply", action="store_true")
    config_set_parser.add_argument("--json", action="store_true")

    # Guidance command
    guidance_parser = subparsers.add_parser(
        "guidance",
        help="Show code execution guidance configuration",
        description="Display current guidance settings and token budget.",
    )
    guidance_parser.add_argument(
        "--show-budget",
        action="store_true",
        help="Show estimated token budget for current config",
    )
    guidance_parser.add_argument(
        "--telemetry",
        choices=["on", "off"],
        help="Persistently enable or disable PMCP feedback telemetry prompts",
    )

    # Doctor command
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose PMCP configuration conflicts",
        description="Detect lock, mode, HTTP reachability, remote header, and install issues.",
    )
    doctor_parser.add_argument(
        "-p",
        "--project",
        type=Path,
        help="Project root directory (defaults to auto-discovery)",
    )
    doctor_parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="HTTP health probe timeout in seconds (default: 3.0)",
    )
    doctor_parser.add_argument(
        "-l",
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="warn",
        help="Log level (default: warn)",
    )

    # Upgrade command
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Upgrade the pmcp package to the latest release",
        description=(
            "Detect how pmcp was installed (uv tool vs pip --user) and "
            "run the matching upgrade command. Optionally restart the "
            "local gateway service afterwards."
        ),
    )
    upgrade_parser.add_argument(
        "--method",
        choices=["auto", "uv", "pip"],
        default="auto",
        help="Override install-method detection (default: auto)",
    )
    upgrade_parser.add_argument(
        "--restart-service",
        action="store_true",
        help="Restart the local systemd/launchd pmcp service after upgrade",
    )
    upgrade_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without invoking the upgrade command",
    )
    upgrade_parser.add_argument(
        "-l",
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="warn",
        help="Log level (default: warn)",
    )

    # Secrets command
    secrets_parser = subparsers.add_parser(
        "secrets",
        help="Manage PMCP secret environment files",
        description="Set, sync, and check secrets in user/project PMCP .env files.",
    )
    secrets_subparsers = secrets_parser.add_subparsers(
        dest="secrets_command",
        required=True,
        help="Secrets subcommands",
    )

    secrets_set_parser = secrets_subparsers.add_parser(
        "set",
        help="Set a secret value in PMCP env file",
    )
    secrets_set_parser.add_argument("key", type=str, help="Secret key name")
    secrets_set_parser.add_argument(
        "value",
        type=str,
        nargs="?",
        default=None,
        help="Secret value. Omit to read it from --stdin or an interactive "
        "prompt; passing it here exposes it in 'ps aux' and shell history.",
    )
    secrets_set_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the secret value from stdin instead of passing it on argv",
    )
    secrets_set_parser.add_argument(
        "--scope",
        choices=["user", "project"],
        default="project",
        help="Where to store the secret (default: project)",
    )
    secrets_set_parser.add_argument(
        "--project",
        type=Path,
        help="Project root directory (for project scope)",
    )

    secrets_sync_parser = secrets_subparsers.add_parser(
        "sync",
        help="Sync secrets between PMCP env scopes",
    )
    secrets_sync_parser.add_argument(
        "--from-scope",
        choices=["user", "project"],
        default="user",
        help="Source scope (default: user)",
    )
    secrets_sync_parser.add_argument(
        "--to-scope",
        choices=["user", "project"],
        default="project",
        help="Target scope (default: project)",
    )
    secrets_sync_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing keys in target scope",
    )
    secrets_sync_parser.add_argument(
        "--project",
        type=Path,
        help="Project root directory (for project scope)",
    )

    secrets_check_parser = secrets_subparsers.add_parser(
        "check",
        help="Check required secrets and availability",
    )
    secrets_check_parser.add_argument(
        "--project",
        type=Path,
        help="Project root directory (for project scope)",
    )

    # Auth command
    auth_parser = subparsers.add_parser(
        "auth",
        help="Authenticate MCP servers and store credentials",
        description="Connect authentication for a server and optionally auto-provision it.",
    )
    auth_subparsers = auth_parser.add_subparsers(
        dest="auth_command",
        required=True,
        help="Auth subcommands",
    )

    auth_connect_parser = auth_subparsers.add_parser(
        "connect",
        help="Store credential for a server and provision it",
    )
    auth_connect_parser.add_argument(
        "server_name", type=str, help="Server name requiring authentication"
    )
    auth_connect_parser.add_argument(
        "--credential",
        type=str,
        help="Credential/token value (if omitted, prompts securely)",
    )
    auth_connect_parser.add_argument(
        "--env-var",
        type=str,
        help="Override target environment variable key",
    )
    auth_connect_parser.add_argument(
        "--scope",
        choices=["user", "project"],
        default="user",
        help="Where to store credential (default: user)",
    )
    auth_connect_parser.add_argument(
        "--no-provision",
        action="store_true",
        help="Only store credentials; do not auto-retry provisioning",
    )
    auth_connect_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )
    auth_ack_parser = auth_subparsers.add_parser(
        "acknowledge",
        help="Acknowledge completed URL-mode elicitation",
    )
    auth_ack_parser.add_argument(
        "server_name", type=str, help="Server name requiring acknowledgement"
    )
    auth_ack_parser.add_argument(
        "--elicitation-id",
        required=True,
        help="URL-mode elicitation identifier",
    )
    auth_ack_parser.add_argument(
        "--elicitation-url",
        help="Sanitized URL-mode elicitation URL",
    )
    auth_ack_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )

    return parser.parse_args()


# `check_staleness` reports each stale server as `(cached_version,
# latest_version or "unknown")`, so this literal is what a package it could not
# resolve surfaces as in the second slot. It is deliberately not
# `refresher._UNIDENTIFIED`: that describes the identity *fields*, a different
# axis from the latest-version slot read here.
_UNRESOLVED_VERSION = "unknown"


async def run_refresh(args: argparse.Namespace) -> None:
    """Run the refresh command."""
    from pmcp.manifest.refresher import (
        check_staleness,
        get_cache_path,
        refresh_all,
    )

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    cache_path = get_cache_path(args.cache_dir)

    if args.check_versions:
        # Just check for updates
        logger.info("Checking for package version updates...")
        stale = await check_staleness(cache_path=cache_path)

        # One dict, two things to tell an operator. An entry whose latest
        # version came back `_UNRESOLVED_VERSION` is one pmcp could not look up
        # at all -- a server launched as `node /opt/srv.js` has no package to
        # resolve -- so its identity can never be confirmed and it would be
        # listed here on every run, permanently, under a `--force` remedy that
        # cannot settle it. `check_staleness` is right to include it (an
        # unconfirmed identity must resolve to refresh, never to "skip the
        # check"); it is the report that has to keep the two apart.
        outdated = {
            name: pair for name, pair in stale.items() if pair[1] != _UNRESOLVED_VERSION
        }
        unresolved = {
            name: pair for name, pair in stale.items() if pair[1] == _UNRESOLVED_VERSION
        }

        if not stale:
            print("All cached descriptions are up to date.")

        if outdated:
            print(f"Found {len(outdated)} servers with newer versions:")
            for name, (old, new) in outdated.items():
                print(f"  {name}: {old} -> {new}")
            print("\nRun 'pmcp refresh --force' to update.")

        if unresolved:
            if outdated:
                print()
            print(
                f"Found {len(unresolved)} servers whose version could not be confirmed:"
            )
            for name, (old, _) in unresolved.items():
                print(f"  {name} (cached at {old})")
            print(
                "\npmcp could not look up a current version for these, so it "
                "cannot tell whether"
            )
            print(
                "their cached descriptions still match what the server runs. "
                "That is not the"
            )
            print(
                "same as being out of date. Their descriptions are "
                "regenerated by the next"
            )
            print("'pmcp refresh'.")
        return

    # Refresh descriptions
    servers = [args.server] if args.server else None

    logger.info("Refreshing capability descriptions...")
    if servers:
        print(f"Refreshing server: {servers[0]}")
    else:
        print("Refreshing all servers in manifest...")

    try:
        cache = await refresh_all(
            cache_path=cache_path,
            force=args.force,
            servers=servers,
        )

        print(f"\nRefreshed {len(cache.servers)} servers:")
        for name, desc in cache.servers.items():
            print(f"  {name}: {len(desc.tools)} tools (v{desc.version})")

        print(f"\nCache saved to: {cache_path}")

    except Exception as e:
        logger.error(f"Refresh failed: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def run_update(args: argparse.Namespace) -> None:
    """Run explicit subordinate MCP update command."""
    from pmcp.client.manager import ClientManager
    from pmcp.policy.policy import PolicyManager
    from pmcp.types import RemoteMcpServerConfig, ResolvedServerConfig

    setup_logging(args.log_level)

    if not args.server and not args.all:
        print("Error: provide a server name or --all", file=sys.stderr)
        sys.exit(2)

    policy_path = args.policy if hasattr(args, "policy") else None
    policy_manager = PolicyManager(policy_path)
    client_manager = ClientManager(
        max_tools_per_server=policy_manager.get_max_tools_per_server()
    )

    gateway_url = _get_gateway_url()
    gateway_config = ResolvedServerConfig(
        name="pmcp-gateway",
        source="custom",
        config=RemoteMcpServerConfig(type="streamable-http", url=gateway_url),
    )

    async def _call(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        raw = await client_manager.call_tool(
            f"pmcp-gateway::{tool_name}", arguments, timeout_ms=60000
        )
        return _extract_tool_payload(raw) or {}

    try:
        errors = await client_manager.connect_all([gateway_config])
        if errors:
            _exit_gateway_unreachable(errors)

        targets: list[str] = []
        if args.all:
            health = await _call("gateway.health", {})
            servers = health.get("servers", [])
            if isinstance(servers, list):
                targets = [
                    str(s.get("name"))
                    for s in servers
                    if isinstance(s, dict) and s.get("name")
                ]
        if args.server:
            targets.append(args.server)

        # Keep insertion order but dedupe
        deduped_targets = list(dict.fromkeys(targets))
        results: list[dict[str, object]] = []
        for server_name in deduped_targets:
            result = await _call("gateway.update_server", {"server_name": server_name})
            results.append(result)

        if args.json:
            print(json.dumps({"results": results}, indent=2))
            return

        if not results:
            print("No servers found to update.")
            return

        for item in results:
            ok = bool(item.get("ok"))
            status = "OK" if ok else "FAILED"
            server = item.get("server", "unknown")
            message = item.get("message", "")
            print(f"[{status}] {server}: {message}")
    finally:
        await client_manager.disconnect_all()


def _get_gateway_url() -> str:
    """Return the PMCP gateway MCP endpoint URL."""
    return os.environ.get(
        "PMCP_GATEWAY_URL",
        os.environ.get("PMCP_STATUS_SSE_URL", "http://127.0.0.1:3344/mcp"),
    )


def _get_gateway_health_url() -> str:
    """Return the unauthenticated PMCP gateway health endpoint URL."""
    parsed = urlsplit(_get_gateway_url())
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _redact_url_credentials(url: str) -> str:
    """Remove URL userinfo before printing diagnostics."""
    return redact_auth_url(url)


def _sanitize_cli_payload(value: Any) -> Any:
    """Recursively sanitize strings in CLI-visible payloads."""
    if isinstance(value, str):
        return sanitize_auth_diagnostic(value)
    if isinstance(value, dict):
        return {str(k): _sanitize_cli_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_cli_payload(v) for v in value]
    return value


def _url_verification_suffix(elicitation: dict[str, Any]) -> str:
    """Qualify a displayed elicitation URL that PMCP did not verify.

    Deliberately provenance-neutral: this is printed both for a URL relayed by a
    downstream server and, on the acknowledge path, for one the operator typed,
    so it must not claim the URL "came from the server". Absent or false
    ``url_verified`` both mean unverified -- an older gateway that does not send
    the field must not be read as a clean bill of health (#211).
    """
    if elicitation.get("url_verified") is True:
        return ""
    return " (destination not verified by pmcp)"


def _format_auth_evidence(
    payload: dict[str, Any],
    *,
    verbose: bool = False,
    semantics: dict[str, Any] | None = None,
) -> str | None:
    """Render structured auth evidence without parsing prose diagnostics."""
    auth_state = payload.get("auth_state")
    if not auth_state or auth_state == "none":
        return None

    parts = [f"auth={sanitize_auth_diagnostic(auth_state)}"]
    auth_event = payload.get("auth_event")
    if auth_event:
        parts.append(f"event={sanitize_auth_diagnostic(auth_event)}")

    missing_env_vars = payload.get("missing_env_vars")
    if isinstance(missing_env_vars, list) and missing_env_vars:
        parts.append("missing_env=" + ",".join(str(v) for v in missing_env_vars))

    auth_challenge = payload.get("auth_challenge")
    if isinstance(auth_challenge, dict):
        missing_scopes = auth_challenge.get("missing_scopes")
        if isinstance(missing_scopes, list) and missing_scopes:
            parts.append("scopes=" + ",".join(str(v) for v in missing_scopes))

    url_elicitations = payload.get("url_elicitations")
    if isinstance(url_elicitations, list) and url_elicitations:
        ids = [
            str(item.get("elicitation_id"))
            for item in url_elicitations
            if isinstance(item, dict) and item.get("elicitation_id")
        ]
        if ids:
            parts.append("elicitations=" + ",".join(ids))
        if any(
            isinstance(item, dict) and item.get("url_verified") is not True
            for item in url_elicitations
        ):
            parts.append("url_destination=unverified")

    next_step = payload.get("next_step")
    if not next_step and semantics:
        info = semantics.get(str(auth_state))
        if isinstance(info, dict):
            next_step = info.get("primary_next_action")
    if verbose and next_step:
        parts.append(f"next={sanitize_auth_diagnostic(str(next_step))}")

    return " ".join(parts)


def _exit_gateway_unreachable(errors: list[str]) -> None:
    """Exit with a clear direct-gateway connection failure."""
    detail = "; ".join(errors)
    print(f"Error: cannot reach PMCP gateway: {detail}", file=sys.stderr)
    sys.exit(1)


def _extract_tool_payload(result: dict[str, object]) -> dict[str, object] | None:
    """Extract JSON payload from MCP tool response content."""
    content = result.get("content")
    if not isinstance(content, list):
        return None

    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def _query_running_gateway_status(
    args: argparse.Namespace, policy_manager: object
) -> dict[str, object] | None:
    """Query live gateway status over streamable HTTP, return None on failure."""
    import time

    from pmcp.client.manager import ClientManager
    from pmcp.types import RemoteMcpServerConfig, ResolvedServerConfig

    logger = logging.getLogger(__name__)
    probe_manager = ClientManager(
        max_tools_per_server=policy_manager.get_max_tools_per_server()  # type: ignore[attr-defined]
    )
    probe_url = _get_gateway_url()
    probe_config = ResolvedServerConfig(
        name="pmcp-gateway",
        source="custom",
        config=RemoteMcpServerConfig(type="streamable-http", url=probe_url),
    )

    try:
        errors = await probe_manager.connect_all([probe_config])
        if errors:
            logger.debug("Live gateway status query failed: %s", "; ".join(errors))
            return None

        health_raw = await probe_manager.call_tool(
            "pmcp-gateway::gateway.health", {}, timeout_ms=3000
        )
        health_payload = _extract_tool_payload(health_raw)
        if not health_payload:
            return None

        servers = health_payload.get("servers", [])
        if not isinstance(servers, list):
            servers = []
        if args.server and isinstance(servers, list):
            servers = [
                s
                for s in servers
                if isinstance(s, dict) and s.get("name") == args.server
            ]

        snapshot: dict[str, object] = {
            "revision_id": health_payload.get("revision_id", ""),
            "last_refresh_ts": health_payload.get("last_refresh_ts", time.time()),
            "servers": servers,
            "total_tools": sum(
                s.get("tool_count", 0)
                for s in servers
                if isinstance(s, dict) and isinstance(s.get("tool_count"), int)
            ),
        }
        if isinstance(health_payload.get("gateway_diagnostics"), dict):
            snapshot["gateway_diagnostics"] = health_payload["gateway_diagnostics"]
        if isinstance(health_payload.get("audit_events"), list):
            snapshot["audit_events"] = health_payload["audit_events"]

        if args.pending:
            pending_raw = await probe_manager.call_tool(
                "pmcp-gateway::gateway.list_pending",
                {"server": args.server} if args.server else {},
                timeout_ms=3000,
            )
            pending_payload = _extract_tool_payload(pending_raw) or {}
            pending_requests = pending_payload.get("requests", [])
            if isinstance(pending_requests, list):
                snapshot["pending_requests"] = pending_requests

        return snapshot
    except Exception:
        logger.debug("Live gateway status query failed", exc_info=True)
        return None
    finally:
        await probe_manager.disconnect_all()


async def run_status(args: argparse.Namespace) -> None:
    """Display gateway and server status."""
    import json
    import time
    from datetime import datetime

    from pmcp.client.manager import ClientManager
    from pmcp.config.loader import load_configs
    from pmcp.identity import filter_self_references
    from pmcp.policy.policy import PolicyManager
    from pmcp.remote_auth import build_remote_header_env_lookup, resolve_remote_headers
    from pmcp.types import RemoteMcpServerConfig, ServerStatusEnum

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Initialize components
    policy_path = args.policy if hasattr(args, "policy") else None
    policy_manager = PolicyManager(policy_path)
    client_manager = ClientManager(
        max_tools_per_server=policy_manager.get_max_tools_per_server()
    )

    live_snapshot = await _query_running_gateway_status(args, policy_manager)
    if live_snapshot is not None:
        live_snapshot = _sanitize_cli_payload(live_snapshot)
        servers = live_snapshot.get("servers", [])
        if not isinstance(servers, list):
            servers = []

        if args.json:
            print(json.dumps(live_snapshot, indent=2))
            return

        online = sum(
            1
            for s in servers
            if isinstance(s, dict) and s.get("status") == ServerStatusEnum.ONLINE.value
        )
        offline = len(servers) - online

        print("PMCP Status")
        print("==================\n")
        print(
            f"PMCP Gateway: reachable ({_redact_url_credentials(_get_gateway_url())})"
        )
        diagnostics = live_snapshot.get("gateway_diagnostics")
        auth_semantics = (
            diagnostics.get("auth_state_semantics")
            if isinstance(diagnostics, dict)
            else None
        )

        if servers:
            print(f"\nDownstream Server State ({online} online, {offline} offline):")
            for s in servers:
                if not isinstance(s, dict):
                    continue
                status = s.get("status", "unknown")
                if status == ServerStatusEnum.ONLINE.value:
                    icon = "\u2713"
                    details = f"{s.get('tool_count', 0):>3} tools"
                else:
                    icon = "\u2717"
                    details = f"({sanitize_auth_diagnostic(s.get('error') or status)})"
                auth_detail = _format_auth_evidence(
                    s,
                    verbose=args.verbose,
                    semantics=auth_semantics
                    if isinstance(auth_semantics, dict)
                    else None,
                )
                if auth_detail:
                    details = f"{details}  {auth_detail}"
                if args.verbose and s.get("startup_policy"):
                    policy_parts = [f"policy={s.get('startup_policy')}"]
                    if s.get("startup_source"):
                        policy_parts.append(f"source={s.get('startup_source')}")
                    if s.get("startup_skip_reason"):
                        policy_parts.append(f"reason={s.get('startup_skip_reason')}")
                    if s.get("startup_env_var"):
                        policy_parts.append(f"env={s.get('startup_env_var')}")
                    policy_missing_env_vars = s.get("missing_env_vars")
                    if (
                        isinstance(policy_missing_env_vars, list)
                        and policy_missing_env_vars
                    ):
                        policy_parts.append(
                            "missing_env="
                            + ",".join(str(v) for v in policy_missing_env_vars)
                        )
                    details = f"{details}  {' '.join(policy_parts)}"
                if args.verbose and s.get("protocol_version"):
                    details = f"{details}  protocol={s.get('protocol_version')}"
                print(f"  {icon} {s.get('name', ''):<16} {status:<10} {details}")
        else:
            print("No servers found.")

        print(f"\nTools: {live_snapshot.get('total_tools', 0)} indexed")
        last_refresh = live_snapshot.get("last_refresh_ts", time.time())
        if isinstance(last_refresh, (int, float)):
            elapsed = time.time() - float(last_refresh)
            if elapsed < 60:
                time_str = "just now"
            elif elapsed < 3600:
                time_str = f"{int(elapsed / 60)} minutes ago"
            else:
                time_str = f"{int(elapsed / 3600)} hours ago"
            print(f"Last refresh: {time_str}")

        if args.pending:
            pending_requests = live_snapshot.get("pending_requests", [])
            if isinstance(pending_requests, list) and pending_requests:
                print(f"\nPending Requests ({len(pending_requests)}):")
                for p in pending_requests:
                    if not isinstance(p, dict):
                        continue
                    warn = " \u26a0" if p.get("state") == "stalled" else ""
                    print(
                        f"  {p.get('tool_id', '')}  {p.get('elapsed_seconds', 0):.1f}s  [{p.get('state', 'unknown')}]{warn}"
                    )
            else:
                print("\nNo pending requests.")

        if args.verbose:
            diagnostics = live_snapshot.get("gateway_diagnostics")
            if isinstance(diagnostics, dict):
                print("\nGateway Diagnostics:")
                transport = diagnostics.get("transport")
                if transport:
                    print(f"  Transport: {transport}")
                header_compat = diagnostics.get("header_compatibility")
                if isinstance(header_compat, dict) and header_compat:
                    states = ", ".join(
                        f"{key}={value}" for key, value in sorted(header_compat.items())
                    )
                    print(f"  Headers: {states}")
                print(
                    "  Trace: "
                    f"{'supported' if diagnostics.get('trace_context_supported') else 'disabled'}"
                )
                if diagnostics.get("audit_enabled"):
                    print(
                        "  Audit: "
                        f"enabled buffer={diagnostics.get('audit_buffer_size', 0)}"
                    )
                if diagnostics.get("rate_limit_enabled"):
                    print(f"  Rate limit: {diagnostics.get('rate_limit_rpm')} rpm")
                if diagnostics.get("auth_metadata_present"):
                    print("  Auth metadata: present")
            print(f"\nRevision: {live_snapshot.get('revision_id', '')}")

        return

    # Load configs
    project_root = args.project if hasattr(args, "project") else None
    config_path = args.config if hasattr(args, "config") else None
    configs = load_configs(project_root=project_root, custom_config_path=config_path)

    # Exclude self-referential gateway entries (e.g. pmcp/mcp-gateway)
    # so `pmcp status` only reports downstream servers.
    configs = filter_self_references(configs, suppress_warnings=True)

    # Filter by policy
    allowed_configs = [c for c in configs if policy_manager.is_server_allowed(c.name)]
    env_lookup = build_remote_header_env_lookup(project_root)
    missing_env_vars_by_server: dict[str, list[str]] = {}
    for config in allowed_configs:
        if isinstance(config.config, RemoteMcpServerConfig):
            resolution = resolve_remote_headers(config.config.headers, env_lookup)
            if resolution.missing_env_vars:
                missing_env_vars_by_server[config.name] = resolution.missing_env_vars

    if not allowed_configs:
        if args.json:
            print(
                json.dumps(
                    {"servers": [], "tools": 0, "message": "No servers configured"}
                )
            )
        else:
            print("No MCP servers configured.")
        return

    try:
        if getattr(args, "probe", False):
            logger.info(f"Connecting to {len(allowed_configs)} servers...")
            await client_manager.connect_all(allowed_configs)
            statuses = client_manager.get_all_server_statuses()
            tools = client_manager.get_all_tools()
        else:
            # Default: lazy view. `status` is a read-only command, so it must not
            # spawn/connect every configured server as a side effect. Use --probe
            # to opt into active connection.
            from pmcp.types import ServerStatus

            statuses = [
                ServerStatus(
                    name=config.name,
                    status=ServerStatusEnum.LAZY,
                    tool_count=0,
                )
                for config in allowed_configs
            ]
            tools = []
        revision_id, last_refresh = client_manager.get_registry_meta()

        # Filter by server if specified
        if args.server:
            statuses = [s for s in statuses if s.name == args.server]
            tools = [t for t in tools if t.server_name == args.server]

        if args.json:
            # JSON output
            output = {
                "revision_id": revision_id,
                "last_refresh_ts": last_refresh,
                "last_refresh_iso": datetime.fromtimestamp(last_refresh).isoformat(),
                "servers": [
                    {
                        "name": s.name,
                        "status": s.status.value,
                        "tool_count": s.tool_count,
                        "resource_count": s.resource_count,
                        "prompt_count": s.prompt_count,
                        "protocol_version": s.protocol_version,
                        "server_capabilities": s.server_capabilities,
                        "last_error": sanitize_auth_diagnostic(s.last_error)
                        if s.last_error
                        else None,
                        "missing_env_vars": missing_env_vars_by_server.get(s.name, []),
                        "auth_state": "missing_auth"
                        if missing_env_vars_by_server.get(s.name)
                        else "none",
                        "pending_requests": s.pending_request_count,
                        "avg_response_time_ms": s.avg_response_time_ms,
                    }
                    for s in statuses
                ],
                "total_tools": len(tools),
            }

            if args.pending:
                pending = client_manager.get_pending_requests(args.server)
                output["pending_requests"] = [
                    {
                        "request_id": f"{p.server_name}::{p.request_id}",
                        "tool_id": p.tool_id,
                        "elapsed_seconds": time.time() - p.started_at,
                        "state": client_manager.get_request_state(p).value,
                    }
                    for p in pending
                ]

            print(json.dumps(output, indent=2))
        else:
            # Human-readable output
            online = sum(1 for s in statuses if s.status == ServerStatusEnum.ONLINE)
            offline = len(statuses) - online

            print("PMCP Status")
            print("==================\n")
            print("PMCP Gateway: local fallback (no live gateway snapshot)")

            if statuses:
                print(
                    f"\nDownstream Server State ({online} online, {offline} offline):"
                )
                for s in statuses:
                    if s.status == ServerStatusEnum.ONLINE:
                        icon = "\u2713"  # checkmark
                        avg_time = (
                            f"{s.avg_response_time_ms:.0f}ms"
                            if s.avg_response_time_ms
                            else "<1s"
                        )
                        details = f"{s.tool_count:>3} tools   {avg_time} avg"
                    else:
                        icon = "\u2717"  # x mark
                        details = f"({sanitize_auth_diagnostic(s.last_error or s.status.value)})"
                    if args.verbose and s.protocol_version:
                        details = f"{details}  protocol={s.protocol_version}"
                    missing_env_vars = missing_env_vars_by_server.get(s.name, [])
                    auth_detail = _format_auth_evidence(
                        {
                            "auth_state": "missing_auth"
                            if missing_env_vars
                            else "none",
                            "missing_env_vars": missing_env_vars,
                            "next_step": f"gateway.auth_connect(server_name='{s.name}')"
                            if missing_env_vars
                            else None,
                        },
                        verbose=args.verbose,
                    )
                    if auth_detail:
                        details = f"{details}  {auth_detail}"

                    print(f"  {icon} {s.name:<16} {s.status.value:<10} {details}")
            else:
                print("No servers found.")

            print(f"\nTools: {len(tools)} indexed")

            # Format last refresh time
            elapsed = time.time() - last_refresh
            if elapsed < 60:
                time_str = "just now"
            elif elapsed < 3600:
                time_str = f"{int(elapsed / 60)} minutes ago"
            else:
                time_str = f"{int(elapsed / 3600)} hours ago"
            print(f"Last refresh: {time_str}")

            if args.pending:
                pending = client_manager.get_pending_requests(args.server)
                if pending:
                    print(f"\nPending Requests ({len(pending)}):")
                    for p in pending:
                        elapsed_s = time.time() - p.started_at
                        state = client_manager.get_request_state(p)
                        warn = " \u26a0" if state.value == "stalled" else ""
                        print(f"  {p.tool_id}  {elapsed_s:.1f}s  [{state.value}]{warn}")
                else:
                    print("\nNo pending requests.")

            if args.verbose:
                print(f"\nRevision: {revision_id}")

    finally:
        # Disconnect from all servers
        await client_manager.disconnect_all()


async def run_logs(args: argparse.Namespace) -> None:
    """View gateway logs."""

    if not LOG_FILE.exists():
        print("No log file found. Start the gateway first to generate logs.")
        print(f"Log file location: {LOG_FILE}")
        return

    def filter_line(line: str) -> bool:
        """Check if line matches filters."""
        if args.level:
            level_upper = args.level.upper()
            if f"[{level_upper}]" not in line:
                return False
        if args.server:
            if args.server not in line:
                return False
        return True

    def read_last_lines(n: int) -> list[str]:
        """Read last n lines from log file."""
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return [line.rstrip() for line in lines[-n:] if filter_line(line)]

    # Show initial lines
    lines = read_last_lines(args.tail)
    for line in lines:
        print(line)

    # Follow mode
    if args.follow:
        print("\n--- Following logs (Ctrl+C to stop) ---\n")
        try:
            last_pos = LOG_FILE.stat().st_size
            while True:
                await asyncio.sleep(0.5)
                current_size = LOG_FILE.stat().st_size
                if current_size > last_pos:
                    with open(LOG_FILE) as f:
                        f.seek(last_pos)
                        new_lines = f.readlines()
                        for line in new_lines:
                            if filter_line(line):
                                print(line.rstrip())
                    last_pos = current_size
                elif current_size < last_pos:
                    # File was rotated
                    last_pos = 0
        except KeyboardInterrupt:
            print("\nStopped following logs.")


async def run_init(args: argparse.Namespace) -> None:
    """Initialize PMCP configuration."""
    import json

    from pmcp.manifest.loader import load_manifest

    project_dir = args.project or Path.cwd()
    config_path = project_dir / ".mcp.json"

    # Check if config already exists
    if config_path.exists() and not args.force:
        print(f"Configuration already exists: {config_path}")
        print("Use --force to overwrite.")
        return

    print("PMCP Configuration\n")

    # Load manifest to get available servers
    try:
        manifest = load_manifest()
        available_servers = list(manifest.servers.keys())
    except Exception:
        manifest = None
        available_servers = []
        print("Warning: Could not load server manifest.")

    # Curated setup starters surfaced during interactive init.
    # These are intentionally high-utility and high-adoption servers.
    recommended_servers = [
        "filesystem",
        "playwright",
        "context7",
        "github",
        "postgres",
        "sqlite",
        "supabase",
        "notion",
        "firecrawl",
        "tavily",
        "browser-use",
        "chrome-devtools",
        "brightdata",
    ]

    selected_servers: dict[str, dict] = {}

    print("Select MCP servers to enable:\n")
    for name in recommended_servers:
        if name not in available_servers or manifest is None:
            continue

        server = manifest.get_server(name)
        if server is None:
            continue

        prompt = f"  Enable {name} ({server.description})? [y/N]: "
        response = input(prompt).strip().lower()

        if response in ("y", "yes"):
            config: dict[str, object] = {
                "command": server.command,
                "args": server.args,
            }

            # Check for API key. Resolve availability through the server's
            # (optionally namespaced) storage key so a credential stored under a
            # secret_key like BRIGHTDATA_API_TOKEN is recognized. Do NOT write a
            # {env_var: "${env_var}"} placeholder: local stdio env is passed
            # verbatim (no ${VAR} expansion), so it would be a dead literal — PMCP
            # resolves the credential from the store at load time instead.
            if server.env_var:
                from pmcp.manifest.loader import (
                    credential_lookup_keys,
                    credential_requirement,
                    credential_storage_key,
                )

                requirement = credential_requirement(server)
                if requirement.required:
                    found_key = next(
                        (
                            k
                            for k in credential_lookup_keys(server)
                            if os.environ.get(k)
                        ),
                        None,
                    )
                    storage_key = credential_storage_key(server) or server.env_var
                    if found_key:
                        print(f"    Found {found_key} in environment")
                    else:
                        print(
                            f"    Note: store the credential with "
                            f"`pmcp secrets set {storage_key} <value>`"
                        )
                else:
                    print(
                        f"    No credential needed — {requirement.relaxed_by} "
                        f"selects a self-hosted endpoint"
                    )

            selected_servers[name] = config
            print(f"    Added {name}")

    if not selected_servers:
        print("\nNo servers selected. Creating minimal config...")
        selected_servers["filesystem"] = {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        }

    # Write config
    config_data = {"mcpServers": selected_servers}

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config_data, f, indent=2)

    print(f"\nConfiguration saved to: {config_path}")
    print(f"Servers configured: {len(selected_servers)}")
    print("\nRun 'pmcp' to start the gateway.")


SETUP_PROFILES: dict[str, dict[str, str]] = {
    "local-stdio": {"mode": "stdio"},
    "shared-local-http": {"mode": "http"},
    "authenticated-shared-http": {"mode": "http", "authenticated": "1"},
    "ci": {"mode": "stdio", "ci": "1"},
}


def _build_setup_config(
    mode: str, client: str, *, authenticated: bool = False, ci: bool = False
) -> dict:
    """Build client config snippet for PMCP."""
    if client == "claude":
        if mode in ("sse", "http"):
            server: dict[str, object] = {
                "type": "http",
                "url": "http://127.0.0.1:3344/mcp",
            }
            if authenticated:
                server["headers"] = {"Authorization": "Bearer ${PMCP_AUTH_TOKEN}"}
            return {
                "mcpServers": {
                    "pmcp": server,
                }
            }
        env = {"PMCP_LOG_LEVEL": "error"} if ci else None
        server = {"command": "pmcp", "args": []}
        if env:
            server["env"] = env
        return {
            "mcpServers": {
                "pmcp": server,
            }
        }

    # OpenCode
    if mode in ("sse", "http"):
        server = {
            "type": "remote",
            "url": "http://127.0.0.1:3344/mcp",
            "enabled": True,
        }
        if authenticated:
            server["headers"] = {"Authorization": "Bearer ${PMCP_AUTH_TOKEN}"}
        return {
            "mcp": {
                "pmcp": server,
            }
        }
    server = {"type": "local", "command": "pmcp", "enabled": True}
    if ci:
        server["environment"] = {"PMCP_LOG_LEVEL": "error"}
    return {
        "mcp": {
            "pmcp": server,
        }
    }


def _setup_profile_options(args: argparse.Namespace) -> tuple[str, bool, bool]:
    if not getattr(args, "profile", None):
        return args.mode, False, False
    profile = SETUP_PROFILES[args.profile]
    return (
        profile.get("mode", args.mode),
        profile.get("authenticated") == "1",
        profile.get("ci") == "1",
    )


def _get_setup_target_path(client: str) -> Path:
    """Get the destination config path for a supported client."""
    home = Path.home()
    if client == "claude":
        return home / ".mcp.json"
    return home / ".config" / "opencode" / "opencode.json"


def _merge_setup_config(existing: dict, generated: dict) -> dict:
    """Merge generated top-level config keys into existing config."""
    merged = dict(existing)
    for key, value in generated.items():
        if isinstance(value, dict):
            current = merged.get(key)
            if not isinstance(current, dict):
                current = {}
            current.update(value)
            merged[key] = current
        else:
            merged[key] = value
    return merged


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON data to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as tmp_file:
        json.dump(data, tmp_file, indent=2)
        tmp_file.write("\n")
        tmp_path = Path(tmp_file.name)
    tmp_path.replace(path)


def run_setup(args: argparse.Namespace) -> None:
    """Render or write PMCP config for a supported client."""
    mode, authenticated, ci = _setup_profile_options(args)
    config = _build_setup_config(
        mode=mode,
        client=args.client,
        authenticated=authenticated,
        ci=ci,
    )

    if not args.write:
        print(json.dumps(config, indent=2))
        return

    target_path = _get_setup_target_path(args.client)
    existing: dict = {}
    if target_path.exists():
        try:
            parsed = json.loads(target_path.read_text())
            if isinstance(parsed, dict):
                existing = parsed
            else:
                raise ValueError("Top-level config must be a JSON object")
        except Exception as exc:
            print(
                f"Error: Could not parse existing config at {target_path}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    merged = _merge_setup_config(existing, config)
    _atomic_write_json(target_path, merged)
    print(f"Wrote PMCP setup to: {target_path}")


def run_config(args: argparse.Namespace) -> None:
    """Run local config administration commands."""
    command = getattr(args, "config_command", None)
    configs = load_configs()
    manifest = load_manifest().servers
    known_names = {config.name for config in configs} | set(manifest)
    if command == "startup-policy":
        policy_output = get_startup_policy(known_server_names=known_names)
        if args.json:
            print(policy_output.model_dump_json(indent=2))
            return
        for source in policy_output.sources:
            print(f"{source.source}: {source.path}")
            print(f"  autoStart: {', '.join(source.autoStart) or '-'}")
            print(f"  disableAutoStart: {', '.join(source.disableAutoStart) or '-'}")
        for diagnostic in policy_output.diagnostics:
            print(f"WARN {diagnostic.code}: {diagnostic.message}")
        return
    if command == "set-startup-policy":
        preview_output = set_startup_policy(
            StartupPolicyOperation(
                operation=args.operation,
                names=args.names,
                source=args.source,
                path=args.path,
                apply=args.apply,
                dry_run=not args.apply,
            )
        )
        if args.json:
            print(preview_output.model_dump_json(indent=2))
            return
        print(preview_output.message)
        if preview_output.path:
            print(f"Source: {preview_output.path}")
        print(f"Before: {', '.join(preview_output.before_autoStart) or '-'}")
        print(f"After: {', '.join(preview_output.after_autoStart) or '-'}")
        for diagnostic in preview_output.diagnostics:
            print(f"WARN {diagnostic.code}: {diagnostic.message}")
        return
    if command == "status":
        policy = get_startup_policy(known_server_names=known_names)
        rows = [
            {
                "name": config.name,
                "source": config.source,
                "startup_policy": "eager"
                if any(config.name in source.autoStart for source in policy.sources)
                else "lazy",
            }
            for config in configs
        ]
        status_output = {
            "servers": rows,
            "diagnostics": [d.model_dump() for d in policy.diagnostics],
        }
        if args.json:
            print(json.dumps(status_output, indent=2))
            return
        for row in rows:
            print(f"{row['name']}: {row['startup_policy']} ({row['source']})")
        for diagnostic in policy.diagnostics:
            print(f"WARN {diagnostic.code}: {diagnostic.message}")
        return
    print("Error: config subcommand required", file=sys.stderr)
    sys.exit(2)


def _is_pmcp_system_service_active() -> bool | None:
    """Return user-service status when systemd is available."""
    if os.name != "posix":
        return None
    if shutil.which("systemctl") is None:
        return None

    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "pmcp"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    return proc.returncode == 0 and proc.stdout.strip() == "active"


def _load_local_mcp_json(project_root: Path | None) -> tuple[Path, dict | None]:
    """Load local .mcp.json, if present and valid JSON."""
    from pmcp.config.loader import find_project_root

    base_dir = project_root or find_project_root(Path.cwd()) or Path.cwd()
    config_path = base_dir / ".mcp.json"
    if not config_path.exists():
        return config_path, None

    try:
        with open(config_path) as f:
            parsed = json.load(f)
            return config_path, parsed if isinstance(parsed, dict) else None
    except Exception:
        return config_path, None


def _extract_mode_signals(
    config_data: dict | None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Extract local command entries and SSE URLs from mcpServers."""
    command_servers: list[str] = []
    sse_endpoints: list[tuple[str, str]] = []

    if not isinstance(config_data, dict):
        return command_servers, sse_endpoints

    servers = config_data.get("mcpServers")
    if not isinstance(servers, dict):
        return command_servers, sse_endpoints

    for name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue
        command = server_cfg.get("command")
        if isinstance(command, str) and command.strip():
            command_servers.append(name)
        if server_cfg.get("type") == "sse":
            url = server_cfg.get("url")
            if isinstance(url, str) and url.strip():
                sse_endpoints.append((name, url.strip()))

    return command_servers, sse_endpoints


async def _probe_sse_endpoint(url: str, timeout_s: float) -> tuple[bool, str]:
    """Probe SSE endpoint and return (ok, details)."""
    import httpx

    try:
        timeout = httpx.Timeout(timeout=timeout_s)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream(
                "GET",
                url,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code == 200:
                    return True, "endpoint responded with HTTP 200"
                return False, f"endpoint returned HTTP {response.status_code}"
    except Exception as exc:
        return False, sanitize_auth_diagnostic(exc)


async def _probe_http_health(timeout_s: float) -> tuple[bool, str, int | None]:
    """Probe gateway /health and return (ok, details, status_code).

    status_code is the HTTP status when the gateway responded (even on an error
    status like 401/403), or None when the gateway was unreachable.
    """
    import httpx

    url = _get_gateway_health_url()
    safe_url = _redact_url_credentials(url)
    try:
        timeout = httpx.Timeout(timeout=timeout_s)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
        if response.status_code == 200:
            detail = f"{safe_url} reachable"
            try:
                payload = response.json()
                diagnostics = payload.get("gateway_diagnostics")
            except Exception:
                payload = {}
                diagnostics = None
            if isinstance(diagnostics, dict):
                transport = diagnostics.get("transport")
                trace = diagnostics.get("trace_context_supported")
                rate = diagnostics.get("rate_limit_rpm")
                parts = []
                if transport:
                    parts.append(f"transport={transport}")
                if trace is not None:
                    parts.append(f"trace={'supported' if trace else 'disabled'}")
                if rate:
                    parts.append(f"rate_limit={rate}rpm")
                if diagnostics.get("auth_state_semantics"):
                    parts.append("auth_semantics=available")
                if diagnostics.get("auth_metadata_present"):
                    parts.append("auth_metadata=present")
                audit_events = payload.get("audit_events")
                if isinstance(audit_events, list):
                    parts.append(f"audit_events={len(audit_events)}")
                if parts:
                    detail = f"{detail} ({', '.join(parts)})"
            return True, detail, 200
        return (
            False,
            f"{safe_url} returned HTTP {response.status_code}",
            response.status_code,
        )
    except Exception as exc:
        return (
            False,
            sanitize_auth_diagnostic(
                f"{safe_url} unreachable ({exc.__class__.__name__}: {exc})"
            ),
            None,
        )


async def run_doctor(args: argparse.Namespace) -> None:
    """Diagnose local PMCP conflict conditions and suggest fixes."""
    setup_logging(args.log_level)

    checks: list[tuple[str, str, str]] = []

    lock_path = Path.home() / ".pmcp" / "gateway.lock"
    if lock_path.exists():
        checks.append(
            (
                "lock",
                "warn",
                "Lock file exists at ~/.pmcp/gateway.lock. If no gateway is running, remove stale lock: rm ~/.pmcp/gateway.lock",
            )
        )
    else:
        checks.append(("lock", "ok", "No gateway lock file detected."))

    service_active = _is_pmcp_system_service_active()
    config_path, config_data = _load_local_mcp_json(
        args.project if hasattr(args, "project") else None
    )
    command_servers, _sse_endpoints = _extract_mode_signals(config_data)

    if service_active and command_servers:
        joined = ", ".join(sorted(command_servers))
        checks.append(
            (
                "mode",
                "fail",
                f"Local config {config_path} uses command mode for [{joined}] while system service is active. Use remote URL instead of command (type: http, url: http://127.0.0.1:3344/mcp).",
            )
        )
    elif service_active:
        checks.append(
            (
                "mode",
                "ok",
                "System service is active and no local command conflict was found.",
            )
        )
    else:
        checks.append(("mode", "ok", "No active PMCP system service detected."))

    probe_result = await _probe_http_health(args.timeout)
    http_ok, http_detail = probe_result[0], probe_result[1]
    http_status = probe_result[2] if len(probe_result) > 2 else None
    if http_ok:
        checks.append(
            ("http", "ok", f"Gateway /health reachability OK: {http_detail}.")
        )
    elif http_status in (401, 403):
        checks.append(
            (
                "http",
                "warn",
                "Gateway is up but /health requires authentication "
                f"(HTTP {http_status}): {http_detail}. The gateway is reachable; "
                "supply credentials (PMCP_AUTH_TOKEN or the configured auth mode) "
                "rather than restarting it.",
            )
        )
    else:
        checks.append(
            (
                "http",
                "warn",
                "Gateway /health reachability warning: "
                f"{http_detail}. Start pmcp --transport http or set "
                "PMCP_GATEWAY_URL if the gateway uses a custom URL.",
            )
        )

    remote_checks = collect_remote_header_diagnostics(config_data)
    if remote_checks:
        checks.extend(remote_checks)
    else:
        checks.append(("remote", "ok", "No remote downstream header issues detected."))

    drift = detect_install_drift()
    if drift.has_drift:
        checks.append(
            (
                "install",
                "warn",
                (
                    f"pmcp is installed by BOTH uv tool ({drift.uv_path}) and "
                    f"pip --user ({drift.pip_user_site}). Whichever shim wins PATH "
                    "masks the other; upgrading one leaves the other stale. Remove "
                    "the inactive copy: 'python -m pip uninstall pmcp' or "
                    "'uv tool uninstall pmcp'."
                ),
            )
        )
    else:
        method = detect_install_method()
        checks.append(
            ("install", "ok", f"pmcp install method: {method} (no drift detected).")
        )

    status_icon = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    print("PMCP Doctor")
    print("===========")
    for check_name, status, message in checks:
        icon = status_icon.get(status, status.upper())
        print(f"[{icon}] {check_name}: {message}")

    if any(status == "fail" for _, status, _ in checks):
        sys.exit(1)


def _restart_local_pmcp_service() -> None:
    """Best-effort restart of the local pmcp service (systemd user or launchd)."""
    if shutil.which("systemctl") and Path("/run/systemd/system").exists():
        probe = subprocess.run(
            ["systemctl", "--user", "is-active", "pmcp.service"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            print("No active pmcp systemd user service found — skipping restart.")
            return
        print("Restarting pmcp systemd user service...")
        subprocess.run(["systemctl", "--user", "restart", "pmcp.service"], check=False)
        return
    if sys.platform == "darwin" and shutil.which("launchctl"):
        label = f"gui/{os.getuid()}/com.user.pmcp"
        print(f"Kickstarting launchd {label}...")
        subprocess.run(["launchctl", "kickstart", "-k", label], check=False)
        return
    print("No supported service manager detected — skipping restart.")


async def run_upgrade(args: argparse.Namespace) -> None:
    """Upgrade the pmcp package using the detected install method."""
    setup_logging(args.log_level)

    method = args.method
    if method == "auto":
        method = detect_install_method()
        if method == "unknown":
            print(
                "error: could not auto-detect install method (neither uv tool nor "
                "pip --user). Pass --method=uv or --method=pip explicitly, or "
                "reinstall pmcp from PyPI.",
                file=sys.stderr,
            )
            sys.exit(2)

    if method == "uv":
        cmd = ["uv", "tool", "upgrade", "pmcp"]
    elif method == "pip":
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-U",
            "--user",
            "--break-system-packages",
            "pmcp",
        ]
    else:
        print(f"error: unsupported --method={method}", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print(f"[dry-run] would run: {' '.join(cmd)}")
        return

    print(f"Upgrading pmcp via {method}: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print(
            f"error: upgrade command failed with exit {result.returncode}",
            file=sys.stderr,
        )
        sys.exit(result.returncode)

    drift = detect_install_drift()
    if drift.has_drift:
        print(
            "\nwarning: pmcp is installed by BOTH uv tool and pip --user. The copy "
            f"that was upgraded is {method}; the other is now stale and will mask "
            "the upgrade if PATH order changes. Run 'pmcp doctor' for details."
        )

    if args.restart_service:
        _restart_local_pmcp_service()


async def run_server(args: argparse.Namespace) -> None:
    """Run the MCP gateway server."""
    from pmcp.server import GatewayServer

    # Check environment variables
    if not args.config and os.environ.get("PMCP_CONFIG"):
        args.config = Path(os.environ["PMCP_CONFIG"])
    if not args.policy and os.environ.get("PMCP_POLICY"):
        args.policy = Path(os.environ["PMCP_POLICY"])
    if not getattr(args, "audit_jsonl", None) and os.environ.get("PMCP_AUDIT_JSONL"):
        args.audit_jsonl = Path(os.environ["PMCP_AUDIT_JSONL"])
    if os.environ.get("PMCP_LOG_LEVEL"):
        args.log_level = os.environ["PMCP_LOG_LEVEL"]

    # Transport environment overrides
    if os.environ.get("PMCP_TRANSPORT"):
        args.transport = os.environ["PMCP_TRANSPORT"]
    if os.environ.get("PMCP_HOST"):
        args.host = os.environ["PMCP_HOST"]
    if os.environ.get("PMCP_PORT"):
        args.port = int(os.environ["PMCP_PORT"])
    # Resolve auth token: --auth-token-file > --auth-token > PMCP_AUTH_TOKEN
    auth_token_from_cli = bool(args.auth_token)
    auth_token_file = getattr(args, "auth_token_file", None)
    if auth_token_file and args.auth_token:
        print(
            "error: --auth-token and --auth-token-file are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(1)
    if auth_token_file:
        try:
            args.auth_token = Path(auth_token_file).read_text().strip()
        except OSError as e:
            print(f"error: Cannot read --auth-token-file: {e}", file=sys.stderr)
            sys.exit(1)
    elif not args.auth_token and os.environ.get("PMCP_AUTH_TOKEN"):
        args.auth_token = os.environ["PMCP_AUTH_TOKEN"]
        auth_token_from_cli = False

    # Resolve tunables with a clear precedence: an explicitly-passed CLI flag
    # (default=None sentinel) wins, then the environment variable, then the
    # built-in default. Using a None sentinel instead of comparing against the
    # default value means an explicit flag equal to the default (e.g.
    # --request-timeout 60) still wins over the environment.
    if getattr(args, "max_concurrent_spawns", None) is None:
        env_spawns = os.environ.get("PMCP_MAX_SPAWNS")
        args.max_concurrent_spawns = int(env_spawns) if env_spawns else 8
    if getattr(args, "rate_limit", None) is None:
        env_rate = os.environ.get("PMCP_RATE_LIMIT")
        args.rate_limit = int(env_rate) if env_rate else 0
    if getattr(args, "request_timeout", None) is None:
        env_timeout = os.environ.get("PMCP_REQUEST_TIMEOUT")
        args.request_timeout = int(env_timeout) if env_timeout else 60

    # Auth / Origin env fallbacks (CLI flags take precedence).
    if not getattr(args, "auth_mode", None) and os.environ.get("PMCP_AUTH_MODE"):
        args.auth_mode = os.environ["PMCP_AUTH_MODE"]
    if not getattr(args, "oauth_issuer", None) and os.environ.get("PMCP_OAUTH_ISSUER"):
        args.oauth_issuer = os.environ["PMCP_OAUTH_ISSUER"]
    if not getattr(args, "oauth_jwks_url", None) and os.environ.get(
        "PMCP_OAUTH_JWKS_URL"
    ):
        args.oauth_jwks_url = os.environ["PMCP_OAUTH_JWKS_URL"]
    if not getattr(args, "oauth_audience", None) and os.environ.get(
        "PMCP_OAUTH_AUDIENCE"
    ):
        args.oauth_audience = os.environ["PMCP_OAUTH_AUDIENCE"]
    if not getattr(args, "required_scopes", None) and os.environ.get(
        "PMCP_REQUIRED_SCOPES"
    ):
        args.required_scopes = [
            s.strip()
            for s in os.environ["PMCP_REQUIRED_SCOPES"].split(",")
            if s.strip()
        ]
    if not getattr(args, "allowed_origins", None) and os.environ.get(
        "PMCP_ALLOWED_ORIGINS"
    ):
        args.allowed_origins = [
            o.strip()
            for o in os.environ["PMCP_ALLOWED_ORIGINS"].split(",")
            if o.strip()
        ]

    # Lock directory - CLI flag takes precedence over env var
    lock_dir = getattr(args, "lock_dir", None)
    if not lock_dir and os.environ.get("PMCP_LOCK_DIR"):
        lock_dir = Path(os.environ["PMCP_LOCK_DIR"])

    # Determine log level
    if args.debug:
        log_level = "debug"
    elif args.quiet:
        log_level = "error"
    else:
        log_level = args.log_level

    setup_logging(log_level, log_format=getattr(args, "log_format", "text"))
    logger = logging.getLogger(__name__)

    if auth_token_from_cli:
        logger.warning(
            "--auth-token passed on CLI; token is visible in 'ps aux'. "
            "Use the PMCP_AUTH_TOKEN environment variable instead."
        )

    logger.info("Starting PMCP...")

    server = GatewayServer(
        project_root=args.project,
        custom_config_path=args.config,
        policy_path=args.policy,
        host=args.host,
        port=args.port,
        lock_dir=lock_dir,
        audit_jsonl=getattr(args, "audit_jsonl", None),
        auth_token=getattr(args, "auth_token", None),
        max_concurrent_spawns=getattr(args, "max_concurrent_spawns", 8),
        rate_limit_rpm=getattr(args, "rate_limit", 0),
        request_timeout=getattr(args, "request_timeout", 60),
        auth_mode=getattr(args, "auth_mode", None),
        resource_server_issuer=getattr(args, "oauth_issuer", None),
        resource_server_jwks_url=getattr(args, "oauth_jwks_url", None),
        resource_server_audience=getattr(args, "oauth_audience", None),
        required_scopes=getattr(args, "required_scopes", None),
        allowed_origins=getattr(args, "allowed_origins", None),
    )

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig: signal.Signals) -> None:
        logger.info(f"Received {sig.name}, shutting down...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal, sig)
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(
                sig, lambda signum, frame: handle_signal(signal.Signals(signum))
            )

    try:
        # Run server with shutdown handling
        server_task = asyncio.create_task(server.run(transport=args.transport))
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [server_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks and await them properly
        for task in pending:
            task.cancel()

        # Wait for cancelled tasks to complete with timeout
        if pending:
            await asyncio.wait(pending, timeout=5.0)

        # Check if server task raised an exception
        if server_task in done:
            exc = server_task.exception()
            if exc:
                raise exc

    except asyncio.CancelledError:
        logger.info("Server cancelled")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


def run_guidance(args: argparse.Namespace) -> None:
    """Show guidance configuration status."""
    from pmcp.config.guidance import load_guidance_config, set_telemetry_enabled

    setup_logging(args.log_level)

    # Persist telemetry setting if requested
    if getattr(args, "telemetry", None):
        enabled = args.telemetry == "on"
        _updated, path = set_telemetry_enabled(enabled)
        state = "enabled" if enabled else "disabled"
        print(f"Telemetry {state} in {path}")

    # Load guidance config
    config = load_guidance_config()

    print("Code Execution Guidance Configuration")
    print("=" * 50)
    print(f"Level: {config.level}")
    print("Config file: ~/.claude/gateway-guidance.yaml")
    print()
    print("Layers:")
    print(f"  L0 MCP Instructions: {'✓' if config.include_mcp_instructions else '✗'}")
    print(f"  L1 Code Hints: {'✓' if config.include_code_hints else '✗'}")
    print(f"  L2 Code Snippets: {'✓' if config.include_code_snippets else '✗'}")
    print(
        f"  L3 Methodology Resource: {'✓' if config.include_methodology_resource else '✗'}"
    )
    print(f"  Feedback Telemetry: {'✓' if config.enable_telemetry else '✗'}")
    print()

    if args.show_budget:
        print("Token Budget (Estimated):")
        print("-" * 50)
        minimal = config.estimated_token_cost(num_search_results=15, num_describes=0)
        standard = config.estimated_token_cost(num_search_results=15, num_describes=1)
        heavy = config.estimated_token_cost(num_search_results=20, num_describes=2)

        print(f"  Minimal workflow (L0 + search): ~{minimal} tokens")
        print(f"  Standard workflow (+ 1 describe): ~{standard} tokens")
        print(f"  Heavy workflow (+ 2 describes): ~{heavy} tokens")
        print()

    print("To change configuration:")
    print("  Edit ~/.claude/gateway-guidance.yaml")
    print("  Or set level: off | minimal | standard")


def run_capabilities(args: argparse.Namespace) -> None:
    """Print the installed capability contract for fail-closed consumers."""
    from pmcp.scoped_advisor_audit import SCOPED_ADVISOR_AUDIT_CAPABILITY

    payload: dict[str, Any] = {
        "pmcp_version": importlib.metadata.version("pmcp"),
        "capabilities": [
            {
                "name": SCOPED_ADVISOR_AUDIT_CAPABILITY,
                "activation_requires": [
                    "explicit_policy",
                    "gateway_tool_filtering",
                    "typed_correlations",
                    "explicit_audit_jsonl",
                    "unique_lock_dir",
                    "terminal_completion_fsync",
                ],
            }
        ],
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"pmcp {payload['pmcp_version']}")
        for capability in payload["capabilities"]:
            print(capability["name"])


def _build_gateway_auth_client(args: argparse.Namespace) -> tuple[Any, Any]:
    from pmcp.client.manager import ClientManager
    from pmcp.policy.policy import PolicyManager
    from pmcp.types import RemoteMcpServerConfig, ResolvedServerConfig

    setup_logging(args.log_level)

    policy_path = args.policy if hasattr(args, "policy") else None
    policy_manager = PolicyManager(policy_path)
    client_manager = ClientManager(
        max_tools_per_server=policy_manager.get_max_tools_per_server()
    )

    gateway_url = _get_gateway_url()
    gateway_config = ResolvedServerConfig(
        name="pmcp-gateway",
        source="custom",
        config=RemoteMcpServerConfig(type="streamable-http", url=gateway_url),
    )
    return client_manager, gateway_config


async def _call_gateway_tool(
    client_manager: Any, tool_name: str, arguments: dict[str, object]
) -> dict[str, object]:
    raw = await client_manager.call_tool(
        f"pmcp-gateway::{tool_name}", arguments, timeout_ms=30000
    )
    payload = _extract_tool_payload(raw)
    return payload or {}


def _resolve_secret_set_value(args: argparse.Namespace) -> str:
    """Resolve the value for `secrets set` without exposing it on argv.

    Precedence: an explicit positional value (with a warning, since it is
    visible in ``ps aux`` and shell history) > ``--stdin`` > interactive
    getpass prompt. Mirrors how `auth connect` reads credentials.
    """
    import getpass

    value = getattr(args, "value", None)
    if value is not None:
        print(
            "warning: secret value passed on the command line is visible in "
            "'ps aux' and shell history; pipe it via --stdin or omit it to be "
            "prompted.",
            file=sys.stderr,
        )
        return value

    if getattr(args, "stdin", False):
        line = sys.stdin.readline()
        return line.rstrip("\r\n")

    return getpass.getpass(f"Enter value for {args.key}: ")


async def run_auth_connect(args: argparse.Namespace) -> None:
    """Connect auth for a server and optionally provision it."""
    import getpass

    client_manager, gateway_config = _build_gateway_auth_client(args)

    try:
        errors = await client_manager.connect_all([gateway_config])
        if errors:
            _exit_gateway_unreachable(errors)

        server_name = args.server_name
        first = await _call_gateway_tool(
            client_manager, "gateway.provision", {"server_name": server_name}
        )

        needs_auth = bool(first.get("needs_api_key") or first.get("auth_required"))
        url_elicitations = first.get("url_elicitations")
        if first.get("auth_state") == "elicitation_required" or url_elicitations:
            if args.credential:
                raise RuntimeError(
                    "URL-mode elicitation does not accept credentials through pmcp auth connect"
                )
            elicitation = (
                url_elicitations[0]
                if isinstance(url_elicitations, list) and url_elicitations
                else {}
            )
            if args.json:
                print(json.dumps({"provision": first}, indent=2))
            else:
                print(first.get("message", "URL-mode elicitation required."))
                if elicitation.get("url"):
                    print(
                        f"URL: {_redact_url_credentials(str(elicitation['url']))}"
                        + _url_verification_suffix(elicitation)
                    )
                if elicitation.get("elicitation_id"):
                    print(f"Elicitation ID: {elicitation['elicitation_id']}")
                next_step = elicitation.get("next_step") or first.get("next_step")
                if next_step:
                    print(f"Next step: {next_step}")
            return

        if not needs_auth:
            if args.json:
                print(json.dumps(first, indent=2))
            else:
                print(first.get("message", "Provision request completed."))
            return

        credential = args.credential
        if not credential:
            prompt = f"Enter credential for {server_name}: "
            credential = getpass.getpass(prompt)
            if not credential:
                raise RuntimeError("No credential provided")

        auth_args: dict[str, object] = {
            "server_name": server_name,
            "credential": credential,
            "scope": args.scope,
        }
        if args.env_var:
            auth_args["env_var"] = args.env_var

        auth_result = await _call_gateway_tool(
            client_manager, "gateway.auth_connect", auth_args
        )

        output: dict[str, object] = {
            "auth": auth_result,
            "provision": None,
        }

        if not args.no_provision:
            second = await _call_gateway_tool(
                client_manager, "gateway.provision", {"server_name": server_name}
            )
            output["provision"] = second

        if args.json:
            print(json.dumps(output, indent=2))
        else:
            print(auth_result.get("message", "Credential stored."))
            if output["provision"]:
                provision = output["provision"]
                if isinstance(provision, dict):
                    print(provision.get("message", "Provision retried."))
    finally:
        await client_manager.disconnect_all()


async def run_auth_acknowledge(args: argparse.Namespace) -> None:
    """Acknowledge completed URL-mode elicitation for a server."""
    client_manager, gateway_config = _build_gateway_auth_client(args)

    if getattr(args, "credential", None):
        raise RuntimeError(
            "URL-mode elicitation acknowledgement does not accept credentials"
        )

    try:
        errors = await client_manager.connect_all([gateway_config])
        if errors:
            _exit_gateway_unreachable(errors)

        auth_args: dict[str, object] = {
            "server_name": args.server_name,
            "auth_mode": "url_elicitation",
            "elicitation_id": args.elicitation_id,
            "consent_acknowledged": True,
        }
        if args.elicitation_url:
            auth_args["elicitation_url"] = args.elicitation_url

        auth_result = await _call_gateway_tool(
            client_manager, "gateway.auth_connect", auth_args
        )
        if args.json:
            print(json.dumps({"auth": auth_result}, indent=2))
        else:
            print(auth_result.get("message", "URL-mode elicitation acknowledged."))
            url_elicitation = auth_result.get("url_elicitation")
            if isinstance(url_elicitation, dict) and url_elicitation.get("url"):
                print(
                    f"URL: {_redact_url_credentials(str(url_elicitation['url']))}"
                    + _url_verification_suffix(url_elicitation)
                )
            if auth_result.get("next_step"):
                print(f"Next step: {auth_result['next_step']}")
    finally:
        await client_manager.disconnect_all()


async def async_main(args: argparse.Namespace) -> None:
    """Async main entry point - dispatch to appropriate command."""
    if args.command == "refresh":
        await run_refresh(args)
    elif args.command == "update":
        await run_update(args)
    elif args.command == "status":
        await run_status(args)
    elif args.command == "logs":
        await run_logs(args)
    elif args.command == "init":
        await run_init(args)
    elif args.command == "setup":
        run_setup(args)  # Synchronous command
    elif args.command == "config":
        run_config(args)  # Synchronous command
    elif args.command == "guidance":
        run_guidance(args)  # Synchronous command
    elif args.command == "capabilities":
        run_capabilities(args)  # Synchronous command
    elif args.command == "doctor":
        await run_doctor(args)
    elif args.command == "upgrade":
        await run_upgrade(args)
    elif args.command == "secrets":
        if args.secrets_command == "set":
            output = await run_secrets_set(args)
        elif args.secrets_command == "sync":
            output = await run_secrets_sync(args)
        elif args.secrets_command == "check":
            output = await run_secrets_check(args)
        else:
            output = {
                "ok": False,
                "command": "secrets",
                "error": f"Unknown secrets subcommand: {args.secrets_command}",
            }
        print(json.dumps(output, indent=2))
    elif args.command == "auth":
        if args.auth_command == "connect":
            await run_auth_connect(args)
        elif args.auth_command == "acknowledge":
            await run_auth_acknowledge(args)
        else:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "command": "auth",
                        "error": f"Unknown auth subcommand: {args.auth_command}",
                    },
                    indent=2,
                )
            )
    else:
        # Default: run server
        await run_server(args)


def load_startup_env(dotenv_path: str | os.PathLike[str] | None = None) -> None:
    """Load the startup env files, recording what the plain ``.env`` introduced.

    These are the loads that used to sit inline in ``main()``. ``load_dotenv``
    is called with its semantics COMPLETELY unchanged -- default path discovery,
    interpolation and ``override=False`` precedence all behave exactly as
    before, and the gateway still reads ``.env`` for its own configuration.

    What is new is that the keys the plain ``.env`` introduced *into this
    process* are recorded, so ``sanitized_subprocess_env`` can keep them out of
    the third-party servers PMCP spawns (Consiliency/pmcp#229, review finding
    S-02). The delta is the provenance: a variable the operator already had in
    their environment is in ``before``, so it is never recorded and never
    stripped -- which is exactly right, because ``override=False`` means such a
    variable did not come from the file.

    The two PMCP-store loads need no recording; ``managed_secret_keys`` already
    covers those keys.

    ``dotenv_path`` is a test seam, and ``None`` -- the production call -- is
    identical to the bare ``load_dotenv()`` this replaced: ``find_dotenv``
    resolves the path by walking up from THIS module's directory either way. It
    exists because that frame-based discovery cannot be pointed at a ``tmp_path``
    (``monkeypatch.chdir`` has no effect on it), and the end-to-end proof of
    #229 has to run this production function rather than call ``load_dotenv``
    itself. ``tests/test_env_leak_229.py`` asserts ``main()`` still passes
    nothing.
    """
    before = set(os.environ)
    load_dotenv(dotenv_path)
    record_dotenv_keys(set(os.environ) - before)
    # Load PMCP credential stores written by auth_connect (don't override already-set vars)
    load_dotenv(Path.home() / ".config" / "pmcp" / "pmcp.env", override=False)
    load_dotenv(Path.cwd() / ".env.pmcp", override=False)


def main() -> None:
    """Main entry point."""
    # Load .env from the current directory or project root, plus the PMCP
    # credential stores -- recording which keys the plain .env introduced so
    # they do not reach downstream servers (Consiliency/pmcp#229).
    load_startup_env()

    args = parse_args()

    # Resolve the `secrets set` value here (not in async_main) so it is never
    # required on argv, while async_main stays a pure dispatcher over args.
    if (
        getattr(args, "command", None) == "secrets"
        and getattr(args, "secrets_command", None) == "set"
    ):
        args.value = _resolve_secret_set_value(args)

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

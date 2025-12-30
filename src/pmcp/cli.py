#!/usr/bin/env python3
"""PMCP CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv


from logging.handlers import RotatingFileHandler

LOG_DIR = Path(".pmcp/logs")
LOG_FILE = LOG_DIR / "gateway.log"


def setup_logging(level: str, log_to_file: bool = True) -> None:
    """Configure logging with optional file output."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    log_format = "[%(asctime)s] [%(levelname)s] %(message)s"
    date_format = "%Y-%m-%dT%H:%M:%S"

    # Log to stderr to avoid interfering with MCP stdio
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        stream=sys.stderr,
    )

    # Also log to file for later viewing with 'mcp-gateway logs'
    if log_to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=1024 * 1024,  # 1MB
                backupCount=5,
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(
                logging.Formatter(log_format, datefmt=date_format)
            )
            logging.getLogger().addHandler(file_handler)
        except Exception:
            # If we can't write logs, continue without file logging
            pass


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="PMCP - Progressive MCP: Minimal context bloat with on-demand tool discovery",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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

    return parser.parse_args()


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
        stale = await check_staleness()

        if not stale:
            print("All cached descriptions are up to date.")
        else:
            print(f"Found {len(stale)} servers with newer versions:")
            for name, (old, new) in stale.items():
                print(f"  {name}: {old} -> {new}")
            print("\nRun 'mcp-gateway refresh --force' to update.")
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


async def run_status(args: argparse.Namespace) -> None:
    """Display gateway and server status."""
    import json
    import time
    from datetime import datetime

    from pmcp.client.manager import ClientManager
    from pmcp.config.loader import load_configs
    from pmcp.policy.policy import PolicyManager
    from pmcp.types import ServerStatusEnum

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Initialize components
    policy_path = args.policy if hasattr(args, "policy") else None
    policy_manager = PolicyManager(policy_path)
    client_manager = ClientManager(
        max_tools_per_server=policy_manager.get_max_tools_per_server()
    )

    # Load configs
    project_root = args.project if hasattr(args, "project") else None
    config_path = args.config if hasattr(args, "config") else None
    configs = load_configs(project_root=project_root, custom_config_path=config_path)

    # Filter by policy
    allowed_configs = [c for c in configs if policy_manager.is_server_allowed(c.name)]

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

    # Connect to servers
    logger.info(f"Connecting to {len(allowed_configs)} servers...")
    await client_manager.connect_all(allowed_configs)

    try:
        statuses = client_manager.get_all_server_statuses()
        tools = client_manager.get_all_tools()
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
                        "last_error": s.last_error,
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

            if statuses:
                print(f"Servers ({online} online, {offline} offline):")
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
                        details = f"({s.last_error or s.status.value})"

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
        available_servers = []
        print("Warning: Could not load server manifest.")

    # Common servers to suggest
    common_servers = [
        ("filesystem", "@modelcontextprotocol/server-filesystem", None),
        (
            "github",
            "@modelcontextprotocol/server-github",
            "GITHUB_PERSONAL_ACCESS_TOKEN",
        ),
        ("postgres", "@modelcontextprotocol/server-postgres", "POSTGRES_URL"),
        ("sqlite", "@modelcontextprotocol/server-sqlite", None),
        ("puppeteer", "@anthropics/mcp-server-puppeteer", None),
    ]

    selected_servers: dict[str, dict] = {}

    print("Select MCP servers to enable:\n")
    for name, package, env_var in common_servers:
        # Check if in manifest or known
        is_available = name in available_servers or package.startswith("@")

        if is_available:
            prompt = f"  Enable {name} ({package})? [y/N]: "
            response = input(prompt).strip().lower()

            if response in ("y", "yes"):
                config: dict = {
                    "command": "npx",
                    "args": ["-y", package],
                }

                # Check for API key
                if env_var:
                    env_value = os.environ.get(env_var)
                    if env_value:
                        print(f"    Found {env_var} in environment")
                        config["env"] = {env_var: f"${{{env_var}}}"}
                    else:
                        print(f"    Note: Set {env_var} in .env for this server")
                        config["env"] = {env_var: f"${{{env_var}}}"}

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
    print("\nRun 'mcp-gateway' to start the gateway.")


async def run_server(args: argparse.Namespace) -> None:
    """Run the MCP gateway server."""
    from pmcp.server import GatewayServer

    # Check environment variables
    if not args.config and os.environ.get("PMCP_CONFIG"):
        args.config = Path(os.environ["PMCP_CONFIG"])
    if not args.policy and os.environ.get("PMCP_POLICY"):
        args.policy = Path(os.environ["PMCP_POLICY"])
    if os.environ.get("PMCP_LOG_LEVEL"):
        args.log_level = os.environ["PMCP_LOG_LEVEL"]

    # Determine log level
    if args.debug:
        log_level = "debug"
    elif args.quiet:
        log_level = "error"
    else:
        log_level = args.log_level

    setup_logging(log_level)
    logger = logging.getLogger(__name__)

    logger.info("Starting PMCP...")

    server = GatewayServer(
        project_root=args.project,
        custom_config_path=args.config,
        policy_path=args.policy,
    )

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig: signal.Signals) -> None:
        logger.info(f"Received {sig.name}, shutting down...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal, sig)

    try:
        # Run server with shutdown handling
        server_task = asyncio.create_task(server.run())
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


async def async_main(args: argparse.Namespace) -> None:
    """Async main entry point - dispatch to appropriate command."""
    if args.command == "refresh":
        await run_refresh(args)
    elif args.command == "status":
        await run_status(args)
    elif args.command == "logs":
        await run_logs(args)
    elif args.command == "init":
        await run_init(args)
    else:
        # Default: run server
        await run_server(args)


def main() -> None:
    """Main entry point."""
    # Load .env file from current directory or project root
    load_dotenv()

    args = parse_args()

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

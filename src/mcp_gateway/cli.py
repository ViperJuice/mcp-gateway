#!/usr/bin/env python3
"""MCP Gateway CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv


def setup_logging(level: str) -> None:
    """Configure logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Log to stderr to avoid interfering with MCP stdio
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="MCP Gateway - A meta-server for minimal Claude Code tool bloat",
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
        default=Path(".mcp-gateway"),
        help="Cache directory (default: .mcp-gateway)",
    )
    refresh_parser.add_argument(
        "-l",
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default="info",
        help="Log level (default: info)",
    )

    return parser.parse_args()


async def run_refresh(args: argparse.Namespace) -> None:
    """Run the refresh command."""
    from mcp_gateway.manifest.refresher import (
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


async def run_server(args: argparse.Namespace) -> None:
    """Run the MCP gateway server."""
    from mcp_gateway.server import GatewayServer

    # Check environment variables
    if not args.config and os.environ.get("MCP_GATEWAY_CONFIG"):
        args.config = Path(os.environ["MCP_GATEWAY_CONFIG"])
    if not args.policy and os.environ.get("MCP_GATEWAY_POLICY"):
        args.policy = Path(os.environ["MCP_GATEWAY_POLICY"])
    if os.environ.get("MCP_GATEWAY_LOG_LEVEL"):
        args.log_level = os.environ["MCP_GATEWAY_LOG_LEVEL"]

    # Determine log level
    if args.debug:
        log_level = "debug"
    elif args.quiet:
        log_level = "error"
    else:
        log_level = args.log_level

    setup_logging(log_level)
    logger = logging.getLogger(__name__)

    logger.info("Starting MCP Gateway...")

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
        if server_task in done and server_task.exception():
            raise server_task.exception()

    except asyncio.CancelledError:
        logger.info("Server cancelled")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


async def async_main(args: argparse.Namespace) -> None:
    """Async main entry point - dispatch to appropriate command."""
    if args.command == "refresh":
        await run_refresh(args)
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

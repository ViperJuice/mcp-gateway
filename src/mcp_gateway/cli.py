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

from mcp_gateway.server import GatewayServer


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
        epilog="""
ENVIRONMENT VARIABLES:
  MCP_GATEWAY_CONFIG      Custom config file path
  MCP_GATEWAY_POLICY      Policy file path
  MCP_GATEWAY_LOG_LEVEL   Log level

CONFIG DISCOVERY:
  The gateway looks for MCP server configs in this order:
  1. .mcp.json in project root (or --project path)
  2. ~/.mcp.json
  3. ~/.claude/.mcp.json
  4. Custom config (via --config or MCP_GATEWAY_CONFIG)

  Project configs take precedence over user configs on name collision.

EXAMPLES:
  # Start with default config discovery
  mcp-gateway

  # Start with custom config
  mcp-gateway --config /path/to/mcp-config.json

  # Start with debug logging
  mcp-gateway --debug
""",
    )

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

    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    """Async main entry point."""
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

        for task in pending:
            task.cancel()

        if shutdown_event.is_set():
            await server.shutdown()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


def main() -> None:
    """Main entry point."""
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

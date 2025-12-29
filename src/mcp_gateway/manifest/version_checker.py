"""Package version checking for npm and PyPI packages."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

import aiohttp

logger = logging.getLogger(__name__)

# Cache for version lookups (avoid repeated network calls)
_version_cache: dict[str, str] = {}


async def get_npm_version(package_name: str, timeout: float = 10.0) -> str | None:
    """
    Get the latest version of an npm package.

    Args:
        package_name: The npm package name (e.g., "@playwright/mcp")
        timeout: Request timeout in seconds

    Returns:
        Version string (e.g., "0.0.19") or None if lookup fails
    """
    cache_key = f"npm:{package_name}"
    if cache_key in _version_cache:
        return _version_cache[cache_key]

    # Handle scoped packages (@org/pkg)
    if package_name.startswith("@"):
        url = f"https://registry.npmjs.org/{package_name.replace('@', '%40').replace('/', '%2F')}"
    else:
        url = f"https://registry.npmjs.org/{package_name}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        f"npm lookup failed for {package_name}: HTTP {resp.status}"
                    )
                    return None

                data = await resp.json()
                version = data.get("dist-tags", {}).get("latest")
                if version:
                    _version_cache[cache_key] = version
                return version

    except asyncio.TimeoutError:
        logger.debug(f"npm lookup timeout for {package_name}")
        return None
    except Exception as e:
        logger.debug(f"npm lookup error for {package_name}: {e}")
        return None


async def get_pypi_version(package_name: str, timeout: float = 10.0) -> str | None:
    """
    Get the latest version of a PyPI package.

    Args:
        package_name: The PyPI package name (e.g., "mcp-server-git")
        timeout: Request timeout in seconds

    Returns:
        Version string (e.g., "2025.12.18") or None if lookup fails
    """
    cache_key = f"pypi:{package_name}"
    if cache_key in _version_cache:
        return _version_cache[cache_key]

    url = f"https://pypi.org/pypi/{package_name}/json"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        f"PyPI lookup failed for {package_name}: HTTP {resp.status}"
                    )
                    return None

                data = await resp.json()
                version = data.get("info", {}).get("version")
                if version:
                    _version_cache[cache_key] = version
                return version

    except asyncio.TimeoutError:
        logger.debug(f"PyPI lookup timeout for {package_name}")
        return None
    except Exception as e:
        logger.debug(f"PyPI lookup error for {package_name}: {e}")
        return None


def detect_package_type(
    command: str, args: list[str]
) -> tuple[Literal["npm", "pypi", "unknown"], str | None]:
    """
    Detect package type and name from server command/args.

    Args:
        command: The server command (e.g., "npx", "uvx")
        args: Command arguments

    Returns:
        Tuple of (package_type, package_name) or ("unknown", None)
    """
    if command in ("npx", "npm"):
        # Find npm package in args (usually after -y flag)
        for i, arg in enumerate(args):
            if arg == "-y":
                continue
            # Skip flags
            if arg.startswith("-"):
                continue
            # Found package name (might have @version suffix)
            pkg = arg.split("@latest")[0] if "@latest" in arg else arg
            # Handle scoped packages like @playwright/mcp
            if pkg.startswith("@") or not pkg.startswith("-"):
                return ("npm", pkg)

    elif command == "uvx":
        # First non-flag argument is the package
        for arg in args:
            if not arg.startswith("-"):
                return ("pypi", arg)

    return ("unknown", None)


async def get_package_version(
    command: str, args: list[str], timeout: float = 10.0
) -> tuple[str | None, Literal["npm", "pypi", "unknown"]]:
    """
    Get the latest version for a package based on its command type.

    Args:
        command: The server command (e.g., "npx", "uvx")
        args: Command arguments
        timeout: Request timeout

    Returns:
        Tuple of (version, package_type)
    """
    pkg_type, pkg_name = detect_package_type(command, args)

    if pkg_type == "npm" and pkg_name:
        version = await get_npm_version(pkg_name, timeout)
        return (version, "npm")
    elif pkg_type == "pypi" and pkg_name:
        version = await get_pypi_version(pkg_name, timeout)
        return (version, "pypi")

    return (None, "unknown")


def is_version_newer(current: str, latest: str) -> bool:
    """
    Compare versions to check if latest is newer than current.

    Handles common version formats:
    - Semver: 1.2.3, 0.0.19
    - Date-based: 2025.12.18

    Args:
        current: Current cached version
        latest: Latest available version

    Returns:
        True if latest > current
    """
    if current == latest:
        return False

    # Try to parse as semver-like (X.Y.Z or X.Y)
    def parse_version(v: str) -> tuple[int, ...]:
        # Remove any non-numeric prefixes (v1.0.0 -> 1.0.0)
        v = re.sub(r"^[vV]", "", v)
        # Extract numeric parts
        parts = re.findall(r"\d+", v)
        return tuple(int(p) for p in parts)

    try:
        current_parts = parse_version(current)
        latest_parts = parse_version(latest)
        return latest_parts > current_parts
    except (ValueError, TypeError):
        # Fall back to string comparison
        return latest > current


def clear_version_cache() -> None:
    """Clear the version cache (useful for testing)."""
    _version_cache.clear()

"""LLM-powered capability summary generation using BAML.

Uses BAML for type-safe prompt management with Anthropic Claude.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmcp.types import ToolInfo

logger = logging.getLogger(__name__)

# Timeout for LLM summarization (seconds)
SUMMARIZE_TIMEOUT = 30


async def summarize_capabilities(tools: list[ToolInfo]) -> str:
    """Use BAML to generate a capability summary.

    Args:
        tools: List of tools to summarize

    Returns:
        Human-readable capability summary

    Raises:
        ImportError: If baml-py is not installed
        TimeoutError: If summarization takes too long
        Exception: If BAML call fails
    """
    # Import here to make dependency optional
    try:
        from pmcp.baml_client import b
        from pmcp.baml_client.types import ToolDescription
    except ImportError as e:
        raise ImportError(
            "baml-py not installed. Install with: pip install baml-py"
        ) from e

    if not tools:
        return "MCP Gateway: No tools available."

    # Convert to BAML types
    tool_descriptions = [
        ToolDescription(
            server_name=t.server_name,
            tool_name=t.tool_name,
            description=t.short_description,
        )
        for t in tools
    ]

    logger.debug("Requesting BAML summary for %d tools", len(tools))

    try:
        async with asyncio.timeout(SUMMARIZE_TIMEOUT):
            result = await b.SummarizeCapabilities(tool_descriptions)
    except TimeoutError:
        logger.warning("BAML summarization timed out after %ds", SUMMARIZE_TIMEOUT)
        raise

    # Format as string
    lines = ["MCP Gateway capabilities:"]
    for cat in result.categories:
        lines.append(f"• {cat.name} ({cat.server}): {cat.summary}")
    lines.append(f"\n{result.usage_hint}")

    summary = "\n".join(lines)
    logger.debug("Generated BAML summary: %d chars", len(summary))
    return summary

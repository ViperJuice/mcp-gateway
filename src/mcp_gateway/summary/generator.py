"""Main entry point for capability summary generation.

Tries LLM summarization first, falls back to templates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp_gateway.summary.template_fallback import template_summary

if TYPE_CHECKING:
    from mcp_gateway.types import ToolInfo

logger = logging.getLogger(__name__)


async def generate_capability_summary(
    tools: list[ToolInfo],
    use_llm: bool = True,
) -> str:
    """Generate a capability summary for MCP tools.

    Attempts LLM-based summarization first (using Claude Agent SDK),
    falls back to template-based summary if unavailable or fails.

    Args:
        tools: List of tools to summarize
        use_llm: Whether to attempt LLM summarization (default True)

    Returns:
        Human-readable capability summary for MCP instructions
    """
    if not tools:
        return (
            "MCP Gateway: No tools currently available.\n"
            "Use gateway.refresh to reload server configurations."
        )

    # Try LLM summarization first
    if use_llm:
        try:
            from mcp_gateway.summary.llm_summarizer import summarize_capabilities

            logger.info("Attempting LLM-based capability summary...")
            summary = await summarize_capabilities(tools)
            logger.info("LLM summary generated successfully")
            return summary

        except ImportError:
            logger.info("claude-agent-sdk not available, using template fallback")
        except TimeoutError:
            logger.warning("LLM summarization timed out, using template fallback")
        except Exception as e:
            logger.warning("LLM summarization failed: %s, using template fallback", e)

    # Fall back to template-based summary
    logger.info("Generating template-based capability summary")
    return template_summary(tools)

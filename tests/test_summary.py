"""Tests for capability summary generation."""

from __future__ import annotations

import pytest

from pmcp.summary.template_fallback import (
    extract_capabilities,
    group_by_server,
    template_summary,
)
from pmcp.summary.generator import generate_capability_summary
from pmcp.types import RiskHint, ToolInfo


def make_tool(
    server: str,
    name: str,
    description: str = "A test tool",
) -> ToolInfo:
    """Create a test tool info."""
    return ToolInfo(
        tool_id=f"{server}::{name}",
        server_name=server,
        tool_name=name,
        description=description,
        short_description=description[:100],
        input_schema={"type": "object", "properties": {}},
        tags=[],
        risk_hint=RiskHint.LOW,
    )


class TestExtractCapabilities:
    """Tests for extract_capabilities function."""

    def test_extracts_navigation_keywords(self) -> None:
        tools = [
            make_tool("browser", "navigate", "Navigate to a URL"),
            make_tool("browser", "goto", "Go to page"),
        ]
        caps = extract_capabilities(tools)
        assert "navigation" in caps

    def test_extracts_interaction_keywords(self) -> None:
        tools = [
            make_tool("browser", "click", "Click an element"),
            make_tool("browser", "type", "Type text"),
        ]
        caps = extract_capabilities(tools)
        assert "interaction" in caps

    def test_extracts_screenshot_keywords(self) -> None:
        tools = [make_tool("browser", "screenshot", "Take a screenshot")]
        caps = extract_capabilities(tools)
        assert "screenshots" in caps

    def test_extracts_file_keywords(self) -> None:
        tools = [
            make_tool("fs", "read_file", "Read a file"),
            make_tool("fs", "write_file", "Write a file"),
        ]
        caps = extract_capabilities(tools)
        assert "file" in caps

    def test_limits_to_five_capabilities(self) -> None:
        tools = [
            make_tool("mixed", "navigate", "Navigate"),
            make_tool("mixed", "click", "Click"),
            make_tool("mixed", "screenshot", "Screenshot"),
            make_tool("mixed", "read_file", "Read file"),
            make_tool("mixed", "search", "Search"),
            make_tool("mixed", "debug", "Debug console"),
            make_tool("mixed", "fetch_docs", "Fetch docs"),
        ]
        caps = extract_capabilities(tools)
        assert len(caps) <= 5

    def test_falls_back_to_verbs_if_no_keywords(self) -> None:
        tools = [
            make_tool("custom", "frobulate", "Does frobulation"),
            make_tool("custom", "bifurcate", "Splits things"),
        ]
        caps = extract_capabilities(tools)
        # Should extract first word of tool names as fallback
        assert len(caps) > 0


class TestGroupByServer:
    """Tests for group_by_server function."""

    def test_groups_tools_by_server(self) -> None:
        tools = [
            make_tool("server1", "tool1"),
            make_tool("server1", "tool2"),
            make_tool("server2", "tool3"),
        ]
        grouped = group_by_server(tools)
        assert len(grouped) == 2
        assert len(grouped["server1"]) == 2
        assert len(grouped["server2"]) == 1

    def test_handles_empty_list(self) -> None:
        grouped = group_by_server([])
        assert grouped == {}


class TestTemplateSummary:
    """Tests for template_summary function."""

    def test_generates_summary_for_tools(self) -> None:
        tools = [
            make_tool("playwright", "navigate", "Navigate to URL"),
            make_tool("playwright", "click", "Click element"),
            make_tool("context7", "search_docs", "Search documentation"),
        ]
        summary = template_summary(tools)

        assert "MCP Gateway capabilities:" in summary
        assert "playwright" in summary
        assert "context7" in summary
        assert "gateway.catalog_search" in summary

    def test_handles_empty_tools(self) -> None:
        summary = template_summary([])
        assert "No tools currently available" in summary
        assert "gateway.refresh" in summary

    def test_includes_tool_counts(self) -> None:
        tools = [
            make_tool("server", "tool1"),
            make_tool("server", "tool2"),
            make_tool("server", "tool3"),
        ]
        summary = template_summary(tools)
        assert "3 tools" in summary


class TestGenerateCapabilitySummary:
    """Tests for main generate_capability_summary function."""

    @pytest.mark.asyncio
    async def test_uses_template_when_llm_disabled(self) -> None:
        tools = [make_tool("server", "tool")]
        summary = await generate_capability_summary(tools, use_llm=False)
        assert "MCP Gateway capabilities:" in summary

    @pytest.mark.asyncio
    async def test_handles_empty_tools(self) -> None:
        summary = await generate_capability_summary([], use_llm=False)
        assert "No tools" in summary

    @pytest.mark.asyncio
    async def test_falls_back_to_template_if_llm_unavailable(self) -> None:
        # When claude-agent-sdk is not installed, should fall back to template
        tools = [make_tool("server", "tool")]
        summary = await generate_capability_summary(tools, use_llm=True)
        # Should still generate something (either LLM or template fallback)
        assert len(summary) > 0

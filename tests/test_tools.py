"""Tests for gateway tools."""

from __future__ import annotations

from typing import Any

import pytest

from pmcp.errors import GatewayException
from pmcp.policy.policy import PolicyManager
from pmcp.tools.handlers import GatewayTools
from pmcp.types import (
    RiskHint,
    ToolInfo,
)


class MockClientManager:
    """Mock client manager for testing."""

    def __init__(self, tools: list[ToolInfo] | None = None) -> None:
        self._tools = {t.tool_id: t for t in (tools or [])}
        self._online_servers: set[str] = set()
        self._revision_id = "test-rev"
        self._last_refresh_ts = 1234567890.0

    def get_all_tools(self) -> list[ToolInfo]:
        return list(self._tools.values())

    def get_tool(self, tool_id: str) -> ToolInfo | None:
        return self._tools.get(tool_id)

    def is_server_online(self, name: str) -> bool:
        return name in self._online_servers

    def set_server_online(self, name: str) -> None:
        self._online_servers.add(name)

    def get_all_server_statuses(self) -> list[Any]:
        return []

    def get_registry_meta(self) -> tuple[str, float]:
        return (self._revision_id, self._last_refresh_ts)

    async def call_tool(
        self, tool_id: str, args: dict[str, Any], timeout_ms: int
    ) -> Any:
        return {"content": [{"type": "text", "text": "result"}]}

    async def refresh(self, configs: list[Any]) -> list[str]:
        return []


def create_mock_tools() -> list[ToolInfo]:
    """Create mock tools for testing."""
    return [
        ToolInfo(
            tool_id="github::create_issue",
            server_name="github",
            tool_name="create_issue",
            description="Create a new issue in a GitHub repository",
            short_description="Create a new issue in a GitHub repository",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body"},
                },
                "required": ["title"],
            },
            tags=["github", "git"],
            risk_hint=RiskHint.HIGH,
        ),
        ToolInfo(
            tool_id="github::list_issues",
            server_name="github",
            tool_name="list_issues",
            description="List issues in a repository",
            short_description="List issues in a repository",
            input_schema={"type": "object", "properties": {}},
            tags=["github", "git", "search"],
            risk_hint=RiskHint.LOW,
        ),
        ToolInfo(
            tool_id="jira::search_issues",
            server_name="jira",
            tool_name="search_issues",
            description="Search for Jira issues using JQL",
            short_description="Search for Jira issues using JQL",
            input_schema={"type": "object", "properties": {}},
            tags=["jira", "search"],
            risk_hint=RiskHint.LOW,
        ),
    ]


class TestCatalogSearch:
    """Tests for catalog_search."""

    @pytest.fixture
    def gateway_tools(self) -> GatewayTools:
        client_manager = MockClientManager(create_mock_tools())
        client_manager.set_server_online("github")
        client_manager.set_server_online("jira")

        policy_manager = PolicyManager()

        return GatewayTools(
            client_manager=client_manager,  # type: ignore
            policy_manager=policy_manager,
        )

    @pytest.mark.asyncio
    async def test_returns_all_tools_with_no_filters(
        self, gateway_tools: GatewayTools
    ) -> None:
        result = await gateway_tools.catalog_search({})

        assert len(result.results) == 3
        assert result.total_available == 3
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_filters_by_server_name(self, gateway_tools: GatewayTools) -> None:
        result = await gateway_tools.catalog_search({"filters": {"server": "github"}})

        assert len(result.results) == 2
        assert all(r.server == "github" for r in result.results)

    @pytest.mark.asyncio
    async def test_filters_by_query(self, gateway_tools: GatewayTools) -> None:
        result = await gateway_tools.catalog_search({"query": "search"})

        assert len(result.results) > 0
        assert any(
            "search" in r.tool_name.lower() or "search" in r.tags
            for r in result.results
        )

    @pytest.mark.asyncio
    async def test_filters_by_risk_level(self, gateway_tools: GatewayTools) -> None:
        result = await gateway_tools.catalog_search({"filters": {"risk_max": "low"}})

        assert all(r.risk_hint == "low" for r in result.results)

    @pytest.mark.asyncio
    async def test_respects_limit(self, gateway_tools: GatewayTools) -> None:
        result = await gateway_tools.catalog_search({"limit": 1})

        assert len(result.results) == 1
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_returns_capability_cards_without_schemas(
        self, gateway_tools: GatewayTools
    ) -> None:
        result = await gateway_tools.catalog_search({})

        for card in result.results:
            assert hasattr(card, "tool_id")
            assert hasattr(card, "short_description")
            # Should not have full schema
            assert not hasattr(card, "input_schema")


class TestDescribe:
    """Tests for describe."""

    @pytest.fixture
    def gateway_tools(self) -> GatewayTools:
        client_manager = MockClientManager(create_mock_tools())
        client_manager.set_server_online("github")
        policy_manager = PolicyManager()

        return GatewayTools(
            client_manager=client_manager,  # type: ignore
            policy_manager=policy_manager,
        )

    @pytest.mark.asyncio
    async def test_returns_schema_card(self, gateway_tools: GatewayTools) -> None:
        result = await gateway_tools.describe({"tool_id": "github::create_issue"})

        assert result.server == "github"
        assert result.tool_name == "create_issue"
        assert "GitHub" in result.description
        assert len(result.args) == 2
        assert any(a.name == "title" and a.required for a in result.args)

    @pytest.mark.asyncio
    async def test_raises_for_unknown_tool(self, gateway_tools: GatewayTools) -> None:
        with pytest.raises(GatewayException) as exc_info:
            await gateway_tools.describe({"tool_id": "unknown::tool"})
        assert exc_info.value.code.value == "E301"  # TOOL_NOT_FOUND


class TestInvoke:
    """Tests for invoke."""

    @pytest.fixture
    def gateway_tools(self) -> GatewayTools:
        client_manager = MockClientManager(create_mock_tools())
        client_manager.set_server_online("github")
        policy_manager = PolicyManager()

        return GatewayTools(
            client_manager=client_manager,  # type: ignore
            policy_manager=policy_manager,
        )

    @pytest.mark.asyncio
    async def test_calls_tool_and_returns_result(
        self, gateway_tools: GatewayTools
    ) -> None:
        result = await gateway_tools.invoke(
            {"tool_id": "github::list_issues", "arguments": {}}
        )

        assert result.ok is True
        assert result.tool_id == "github::list_issues"

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_tool(
        self, gateway_tools: GatewayTools
    ) -> None:
        result = await gateway_tools.invoke(
            {"tool_id": "unknown::tool", "arguments": {}}
        )

        assert result.ok is False
        assert "Tool not found" in (result.errors or [])[0]


class TestHealth:
    """Tests for health."""

    @pytest.fixture
    def gateway_tools(self) -> GatewayTools:
        client_manager = MockClientManager(create_mock_tools())
        policy_manager = PolicyManager()

        return GatewayTools(
            client_manager=client_manager,  # type: ignore
            policy_manager=policy_manager,
        )

    @pytest.mark.asyncio
    async def test_returns_health_status(self, gateway_tools: GatewayTools) -> None:
        result = await gateway_tools.health()

        assert result.revision_id == "test-rev"
        assert isinstance(result.servers, list)

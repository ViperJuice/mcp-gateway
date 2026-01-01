"""Tests for guidance configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pmcp.config.guidance import (
    GuidanceConfig,
    GuidanceLayers,
    create_default_guidance_config,
    load_guidance_config,
)
from pmcp.manifest.code_patterns_loader import CodePatternsLoader, get_code_hint
from pmcp.templates.code_snippets_loader import (
    CodeSnippetsLoader,
    get_code_snippet,
)
from pmcp.types import ToolInfo


class TestGuidanceConfigDefaults:
    """Tests for default GuidanceConfig values."""

    def test_default_level_is_minimal(self) -> None:
        config = GuidanceConfig()
        assert config.level == "minimal"

    def test_default_layers_are_correct(self) -> None:
        config = GuidanceConfig()
        assert config.layers.mcp_instructions is True
        assert config.layers.code_hints is True
        assert config.layers.code_snippets is False  # OFF by default
        assert config.layers.methodology_resource is True

    def test_default_max_hint_length(self) -> None:
        config = GuidanceConfig()
        assert config.max_hint_length == 8

    def test_default_max_snippet_lines(self) -> None:
        config = GuidanceConfig()
        assert config.max_snippet_lines == 4


class TestGuidanceConfigLevels:
    """Tests for guidance configuration levels."""

    def test_off_level_disables_all_layers(self) -> None:
        config = GuidanceConfig(level="off")
        assert config.layers.mcp_instructions is False
        assert config.layers.code_hints is False
        assert config.layers.code_snippets is False
        assert config.layers.methodology_resource is False

    def test_minimal_level_enables_l0_l1_l3(self) -> None:
        config = GuidanceConfig(level="minimal")
        assert config.layers.mcp_instructions is True
        assert config.layers.code_hints is True
        assert config.layers.code_snippets is False  # L2 OFF
        assert config.layers.methodology_resource is True

    def test_standard_level_enables_all_layers(self) -> None:
        config = GuidanceConfig(level="standard")
        assert config.layers.mcp_instructions is True
        assert config.layers.code_hints is True
        assert config.layers.code_snippets is True
        assert config.layers.methodology_resource is True


class TestGuidanceConfigLoading:
    """Tests for loading GuidanceConfig from YAML."""

    def test_loads_minimal_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "guidance.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "guidance": {
                        "level": "minimal",
                        "layers": {
                            "mcp_instructions": True,
                            "code_hints": True,
                            "code_snippets": False,
                            "methodology_resource": True,
                        },
                    }
                }
            )
        )

        config = load_guidance_config(config_file)
        assert config.level == "minimal"
        assert config.layers.code_snippets is False

    def test_loads_standard_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "guidance.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "guidance": {
                        "level": "standard",
                        "layers": {"code_snippets": True},
                    }
                }
            )
        )

        config = load_guidance_config(config_file)
        assert config.level == "standard"
        assert config.layers.code_snippets is True

    def test_loads_off_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "guidance.yaml"
        config_file.write_text(yaml.dump({"guidance": {"level": "off"}}))

        config = load_guidance_config(config_file)
        assert config.level == "off"
        assert config.include_code_hints is False

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        config = load_guidance_config(tmp_path / "nonexistent.yaml")
        # Should return defaults
        assert config.level == "minimal"

    def test_handles_malformed_yaml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("{ invalid yaml: [")

        config = load_guidance_config(config_file)
        # Should return defaults on parse error
        assert config.level == "minimal"

    def test_handles_empty_yaml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")

        config = load_guidance_config(config_file)
        # Should return defaults when empty
        assert config.level == "minimal"

    def test_handles_missing_guidance_key(self, tmp_path: Path) -> None:
        config_file = tmp_path / "no-guidance.yaml"
        config_file.write_text(yaml.dump({"other_config": "value"}))

        config = load_guidance_config(config_file)
        # Should return defaults when "guidance" key missing
        assert config.level == "minimal"


class TestTokenBudgetEstimation:
    """Tests for token budget estimation."""

    def test_minimal_mode_budget(self) -> None:
        config = GuidanceConfig(level="minimal")
        budget = config.estimated_token_cost(num_search_results=15, num_describes=0)
        # L0 (~30) + L1 (~10 * 15 cards) = ~180
        assert 150 <= budget <= 250

    def test_standard_mode_budget(self) -> None:
        config = GuidanceConfig(level="standard")
        budget = config.estimated_token_cost(num_search_results=15, num_describes=1)
        # L0 (~30) + L1 (~10 * 15) + L2 (~60 * 1) = ~240
        assert 200 <= budget <= 350

    def test_off_mode_budget(self) -> None:
        config = GuidanceConfig(level="off")
        budget = config.estimated_token_cost(num_search_results=15, num_describes=1)
        assert budget == 0

    def test_budget_scales_with_search_results(self) -> None:
        config = GuidanceConfig(level="minimal")
        small = config.estimated_token_cost(num_search_results=5, num_describes=0)
        large = config.estimated_token_cost(num_search_results=20, num_describes=0)
        # Larger search should cost more
        assert large > small

    def test_budget_scales_with_describes(self) -> None:
        config = GuidanceConfig(level="standard")
        no_describes = config.estimated_token_cost(
            num_search_results=10, num_describes=0
        )
        with_describes = config.estimated_token_cost(
            num_search_results=10, num_describes=3
        )
        # More describes should cost more (L2 enabled in standard)
        assert with_describes > no_describes


class TestCodePatternHints:
    """Tests for code pattern hint matching."""

    def test_exact_tool_id_override(self) -> None:
        # This tool has an override in code_patterns.yaml
        hint = get_code_hint(
            "playwright::browser_navigate",
            "browser_navigate",
            "Navigate browser to URL",
        )
        assert hint is not None
        assert isinstance(hint, str)
        assert len(hint) <= 12  # Should be terse

    def test_keyword_matching_list(self) -> None:
        # Test keyword-based matching for tools with "list"
        hint = get_code_hint("github::list_issues", "list_issues", "List all issues")
        # "list" keyword should match "loop" pattern
        assert hint is not None

    def test_keyword_matching_search(self) -> None:
        # Test keyword matching for "search"
        hint = get_code_hint(
            "jira::search_issues", "search_issues", "Search for issues"
        )
        assert hint is not None

    def test_keyword_matching_find(self) -> None:
        # Test keyword matching for "find" (which is in filter pattern keywords)
        hint = get_code_hint(
            "api::find_results", "find_results", "Find matching results"
        )
        assert hint is not None
        # "find" is in filter pattern keywords

    def test_returns_result_for_unknown_tools(self) -> None:
        # Unknown tools should still get a hint (default or None)
        hint = get_code_hint(
            "unknown::unknown_tool", "unknown_tool", "Does something unknown"
        )
        # Should return None or a valid hint
        assert hint is None or (isinstance(hint, str) and len(hint) <= 12)

    def test_loader_with_custom_patterns_file(self, tmp_path: Path) -> None:
        # Test loading custom patterns file
        patterns_file = tmp_path / "custom_patterns.yaml"
        patterns_file.write_text(
            yaml.dump(
                {
                    "patterns": {
                        "test_pattern": {
                            "hint": "test",
                            "keywords": ["test_keyword"],
                        }
                    },
                    "overrides": {"custom::tool": "custom"},
                    "default_hint": "default",
                }
            )
        )

        loader = CodePatternsLoader(patterns_path=patterns_file)
        hint = loader.get_hint_for_tool("custom::tool", "tool", "description")
        assert hint == "custom"

    def test_loader_handles_missing_patterns_file(self, tmp_path: Path) -> None:
        # Should not crash with missing file
        loader = CodePatternsLoader(patterns_path=tmp_path / "nonexistent.yaml")
        hint = loader.get_hint_for_tool("tool::id", "tool", "description")
        # Should return None or default
        assert hint is None or isinstance(hint, str)


class TestCodeSnippetLoading:
    """Tests for code snippet template loading."""

    def test_loads_static_snippets(self) -> None:
        # Should load from code_examples.yaml
        snippet = get_code_snippet("playwright::browser_navigate", max_lines=4)
        assert snippet is not None
        assert "gateway.invoke" in snippet
        lines = snippet.split("\n")
        assert len(lines) <= 4

    def test_truncates_long_snippets(self) -> None:
        snippet = get_code_snippet("playwright::browser_navigate", max_lines=2)
        if snippet:
            lines = snippet.split("\n")
            assert len(lines) <= 2

    def test_returns_none_for_unknown_tools_without_llm(self) -> None:
        snippet = get_code_snippet(
            "unknown::unknown_tool", max_lines=4, use_llm_fallback=False
        )
        # Should return None without LLM fallback
        assert snippet is None

    def test_loader_with_custom_templates_file(self, tmp_path: Path) -> None:
        # Test loading custom templates
        templates_file = tmp_path / "custom_examples.yaml"
        templates_file.write_text(
            yaml.dump(
                {
                    "custom::tool": {
                        "snippet": "line1\nline2\nline3",
                    }
                }
            )
        )

        loader = CodeSnippetsLoader(templates_path=templates_file)
        snippet = loader.get_snippet_for_tool("custom::tool", max_lines=4)
        assert snippet is not None
        assert "line1" in snippet

    def test_loader_handles_missing_templates_file(self, tmp_path: Path) -> None:
        # Should not crash with missing file
        loader = CodeSnippetsLoader(templates_path=tmp_path / "nonexistent.yaml")
        snippet = loader.get_snippet_for_tool("tool::id", max_lines=4)
        # Should return None
        assert snippet is None

    def test_llm_fallback_with_tool_info(self, mocker) -> None:
        # Mock BAML client - need to mock the import inside the function
        try:
            # Try importing BAML to see if it's available
            from baml_client.sync_client import b  # type: ignore

            # If import succeeds, mock it
            mock_result = mocker.MagicMock()
            mock_result.snippet = (
                "mcp.call_tool('gateway.invoke', ...)\nresult = response"
            )
            mocker.patch.object(b, "GenerateCodeSnippet", return_value=mock_result)

            tool_info = ToolInfo(
                tool_id="custom::new_tool",
                server_name="custom",
                tool_name="new_tool",
                description="A new tool",
                short_description="A new tool",
                input_schema={"type": "object", "properties": {}},
                tags=["custom"],
                risk_hint="low",
            )

            snippet = get_code_snippet(
                "custom::new_tool",
                max_lines=4,
                tool_info=tool_info,
                use_llm_fallback=True,
            )

            # Should get generated snippet
            assert snippet is not None
            assert "gateway.invoke" in snippet
        except ImportError:
            # BAML not available, skip test
            pytest.skip("BAML client not available")

    def test_graceful_failure_when_baml_unavailable(self) -> None:
        # Test when BAML not installed
        tool_info = ToolInfo(
            tool_id="custom::new_tool",
            server_name="custom",
            tool_name="new_tool",
            description="A new tool",
            short_description="A new tool",
            input_schema={"type": "object", "properties": {}},
            tags=["custom"],
            risk_hint="low",
        )

        # Should not crash, just return None or a snippet
        try:
            snippet = get_code_snippet(
                "nonexistent::tool", tool_info=tool_info, use_llm_fallback=True
            )
            # May be None or may succeed if BAML is available
            # Just ensure no exception raised
            assert snippet is None or isinstance(snippet, str)
        except ImportError:
            # Expected if BAML not available
            pass


class TestGuidanceConfigProperties:
    """Tests for GuidanceConfig computed properties."""

    def test_include_mcp_instructions_minimal(self) -> None:
        config = GuidanceConfig(level="minimal")
        assert config.include_mcp_instructions is True

    def test_include_mcp_instructions_off(self) -> None:
        config = GuidanceConfig(level="off")
        assert config.include_mcp_instructions is False

    def test_include_code_hints_minimal(self) -> None:
        config = GuidanceConfig(level="minimal")
        assert config.include_code_hints is True

    def test_include_code_hints_off(self) -> None:
        config = GuidanceConfig(level="off")
        assert config.include_code_hints is False

    def test_include_code_snippets_minimal(self) -> None:
        config = GuidanceConfig(level="minimal")
        assert config.include_code_snippets is False  # OFF in minimal

    def test_include_code_snippets_standard(self) -> None:
        config = GuidanceConfig(level="standard")
        assert config.include_code_snippets is True

    def test_include_methodology_resource_minimal(self) -> None:
        config = GuidanceConfig(level="minimal")
        assert config.include_methodology_resource is True

    def test_include_methodology_resource_off(self) -> None:
        config = GuidanceConfig(level="off")
        assert config.include_methodology_resource is False


class TestCreateDefaultGuidanceConfig:
    """Tests for creating default guidance configuration files."""

    def test_creates_config_file(self, tmp_path: Path) -> None:
        config_path = tmp_path / "test-guidance.yaml"
        result = create_default_guidance_config(output_path=config_path)

        assert result == config_path
        assert config_path.exists()

    def test_created_config_is_valid(self, tmp_path: Path) -> None:
        config_path = tmp_path / "test-guidance.yaml"
        create_default_guidance_config(output_path=config_path)

        # Load the created config
        config = load_guidance_config(config_path)
        assert config.level == "minimal"
        assert config.layers.mcp_instructions is True

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested_path = tmp_path / "nested" / "path" / "guidance.yaml"
        result = create_default_guidance_config(output_path=nested_path)

        assert result == nested_path
        assert nested_path.exists()
        assert nested_path.parent.exists()


class TestGuidanceConfigEdgeCases:
    """Tests for edge cases and error handling."""

    def test_custom_max_hint_length(self) -> None:
        config = GuidanceConfig(max_hint_length=5)
        assert config.max_hint_length == 5

    def test_custom_max_snippet_lines(self) -> None:
        config = GuidanceConfig(max_snippet_lines=10)
        assert config.max_snippet_lines == 10

    def test_level_preset_overrides_layer_config(self) -> None:
        # Level preset should override individual layer settings
        config = GuidanceConfig(
            level="off",
            layers=GuidanceLayers(code_hints=True),  # Try to enable
        )
        # "off" level should disable all layers
        assert config.layers.code_hints is False

    def test_minimal_level_keeps_snippets_off(self) -> None:
        config = GuidanceConfig(
            level="minimal",
            layers=GuidanceLayers(code_snippets=True),  # Try to enable
        )
        # "minimal" level should keep snippets OFF
        assert config.layers.code_snippets is False

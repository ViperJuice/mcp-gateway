"""Code snippet template loader.

This module loads code snippet templates from YAML for L2 guidance.
Falls back to BAML-based LLM generation for tools without static templates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pmcp.types import ToolInfo

logger = logging.getLogger(__name__)


class CodeSnippetsLoader:
    """Loads code snippet templates for tools."""

    def __init__(self, templates_path: Path | None = None):
        """Initialize the code snippets loader.

        Args:
            templates_path: Path to code_examples.yaml file. If None, uses default.
        """
        if templates_path is None:
            templates_path = Path(__file__).parent / "code_examples.yaml"

        self._templates_path = templates_path
        self._snippets: dict[str, str] = {}
        self._generic_fallback: str | None = None

        self._load_snippets()

    def _load_snippets(self) -> None:
        """Load code snippets from YAML file."""
        if not self._templates_path.exists():
            # No templates file, use empty defaults
            return

        try:
            with open(self._templates_path) as f:
                data = yaml.safe_load(f)

            if not data:
                return

            # Load tool-specific snippets
            for tool_id, template_data in data.items():
                if tool_id == "_generic_fallback":
                    self._generic_fallback = template_data.get("snippet", "").strip()
                else:
                    snippet = template_data.get("snippet", "").strip()
                    if snippet:
                        self._snippets[tool_id] = snippet

        except Exception as e:
            # If loading fails, log warning but continue with empty snippets
            print(
                f"Warning: Failed to load code snippets from {self._templates_path}: {e}"
            )

    def get_snippet_for_tool(
        self,
        tool_id: str,
        max_lines: int = 4,
        tool_info: ToolInfo | None = None,
        use_llm_fallback: bool = False,
    ) -> str | None:
        """Get code snippet for a tool.

        Args:
            tool_id: Full tool ID (e.g., "playwright::browser_navigate")
            max_lines: Maximum number of lines to return
            tool_info: Optional ToolInfo for LLM generation fallback
            use_llm_fallback: Whether to use BAML/LLM generation if no static template

        Returns:
            Code snippet string or None if no template exists
        """
        # Check for exact match in static templates
        snippet = self._snippets.get(tool_id)

        if snippet:
            # Trim static template to max lines
            lines = snippet.split("\n")
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                snippet = "\n".join(lines)
            return snippet

        # No static template - try LLM generation if enabled
        if use_llm_fallback and tool_info:
            try:
                return self._generate_snippet_with_llm(tool_info, max_lines)
            except Exception as e:
                logger.warning(
                    f"Failed to generate code snippet for {tool_id} via LLM: {e}"
                )
                return None

        # No template and LLM disabled
        return None

    def _generate_snippet_with_llm(
        self, tool_info: ToolInfo, max_lines: int
    ) -> str | None:
        """Generate code snippet using BAML/LLM.

        Args:
            tool_info: Tool information for generation
            max_lines: Maximum number of lines

        Returns:
            Generated snippet or None if generation fails
        """
        try:
            # Import BAML client (only when needed to avoid hard dependency)
            from baml_client.sync_client import b  # type: ignore

            # Prepare tool data for BAML
            tool_args = []
            if tool_info.input_schema:
                properties = tool_info.input_schema.get("properties", {})
                required = tool_info.input_schema.get("required", [])

                for name, prop in properties.items():
                    tool_args.append(
                        {
                            "name": name,
                            "type": prop.get("type", "unknown"),
                            "required": name in required,
                            "description": prop.get("description", ""),
                        }
                    )

            tool_data = {
                "tool_id": tool_info.tool_id,
                "tool_name": tool_info.tool_name,
                "description": tool_info.description,
                "args": tool_args,
            }

            # Call BAML function to generate snippet
            result = b.GenerateCodeSnippet(tool=tool_data)

            if result and result.snippet:
                # Trim to max lines
                lines = result.snippet.strip().split("\n")
                if len(lines) > max_lines:
                    lines = lines[:max_lines]
                return "\n".join(lines)

            return None

        except ImportError:
            # BAML client not generated yet
            logger.debug(
                "BAML client not available. Run 'baml generate' to enable LLM-based snippet generation."
            )
            return None
        except Exception as e:
            logger.warning(f"LLM snippet generation failed: {e}")
            return None


# Global instance (lazy-loaded)
_code_snippets_loader: CodeSnippetsLoader | None = None


def get_code_snippets_loader() -> CodeSnippetsLoader:
    """Get the global code snippets loader instance."""
    global _code_snippets_loader
    if _code_snippets_loader is None:
        _code_snippets_loader = CodeSnippetsLoader()
    return _code_snippets_loader


def get_code_snippet(
    tool_id: str,
    max_lines: int = 4,
    tool_info: ToolInfo | None = None,
    use_llm_fallback: bool = False,
) -> str | None:
    """Get code snippet for a tool (convenience function).

    Args:
        tool_id: Full tool ID (e.g., "playwright::browser_navigate")
        max_lines: Maximum number of lines to return
        tool_info: Optional ToolInfo for LLM generation fallback
        use_llm_fallback: Whether to use BAML/LLM generation if no static template

    Returns:
        Code snippet string or None if no template exists
    """
    loader = get_code_snippets_loader()
    return loader.get_snippet_for_tool(tool_id, max_lines, tool_info, use_llm_fallback)

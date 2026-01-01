"""Code pattern hint loader and matcher.

This module loads code pattern hints from YAML and matches them to tools
to provide L1 guidance in capability cards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class CodePatternsLoader:
    """Loads and matches code pattern hints to tools."""

    def __init__(self, patterns_path: Path | None = None):
        """Initialize the code patterns loader.

        Args:
            patterns_path: Path to code_patterns.yaml file. If None, uses default.
        """
        if patterns_path is None:
            patterns_path = Path(__file__).parent / "code_patterns.yaml"

        self._patterns_path = patterns_path
        self._patterns: dict[str, Any] = {}
        self._overrides: dict[str, str] = {}
        self._keyword_patterns: dict[str, str] = {}
        self._default_hint: str | None = None

        self._load_patterns()

    def _load_patterns(self) -> None:
        """Load code patterns from YAML file."""
        if not self._patterns_path.exists():
            # No patterns file, use empty defaults
            return

        try:
            with open(self._patterns_path) as f:
                data = yaml.safe_load(f)

            if not data:
                return

            # Load pattern definitions
            self._patterns = data.get("patterns", {})

            # Build keyword -> hint mapping
            for pattern_name, pattern_data in self._patterns.items():
                hint = pattern_data.get("hint")
                keywords = pattern_data.get("keywords", [])
                for keyword in keywords:
                    self._keyword_patterns[keyword.lower()] = hint

            # Load tool-specific overrides
            self._overrides = data.get("overrides", {})

            # Load default hint
            self._default_hint = data.get("default_hint")

        except Exception as e:
            # If loading fails, log warning but continue with empty patterns
            print(
                f"Warning: Failed to load code patterns from {self._patterns_path}: {e}"
            )

    def get_hint_for_tool(
        self, tool_id: str, tool_name: str, description: str
    ) -> str | None:
        """Get code hint for a tool.

        Checks in order:
        1. Tool-specific override by exact tool_id
        2. Keyword matching in tool_name and description
        3. Default hint

        Args:
            tool_id: Full tool ID (e.g., "playwright::browser_navigate")
            tool_name: Tool name (e.g., "browser_navigate")
            description: Tool description

        Returns:
            Code hint string (e.g., "loop") or None if no match
        """
        # Check exact override first
        if tool_id in self._overrides:
            return self._overrides[tool_id]

        # Check keyword matching
        text = f"{tool_name} {description}".lower()
        for keyword, hint in self._keyword_patterns.items():
            if keyword in text:
                return hint

        # Return default hint
        return self._default_hint


# Global instance (lazy-loaded)
_code_patterns_loader: CodePatternsLoader | None = None


def get_code_patterns_loader() -> CodePatternsLoader:
    """Get the global code patterns loader instance."""
    global _code_patterns_loader
    if _code_patterns_loader is None:
        _code_patterns_loader = CodePatternsLoader()
    return _code_patterns_loader


def get_code_hint(tool_id: str, tool_name: str, description: str) -> str | None:
    """Get code hint for a tool (convenience function).

    Args:
        tool_id: Full tool ID (e.g., "playwright::browser_navigate")
        tool_name: Tool name (e.g., "browser_navigate")
        description: Tool description

    Returns:
        Code hint string (e.g., "loop") or None if no match
    """
    loader = get_code_patterns_loader()
    return loader.get_hint_for_tool(tool_id, tool_name, description)

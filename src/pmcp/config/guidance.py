"""Guidance configuration for code execution patterns.

This module handles loading and managing configuration for the code execution
guidance system that helps models use PMCP more effectively.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class GuidanceLayers(BaseModel):
    """Configuration for individual guidance layers."""

    mcp_instructions: bool = Field(
        default=True,
        description="L0: Philosophy in MCP server instructions (~25-35 tokens)",
    )
    code_hints: bool = Field(
        default=True,
        description="L1: Single-word hints in capability cards (~8-12 tokens/card)",
    )
    code_snippets: bool = Field(
        default=False,
        description="L2: Minimal code examples in schema cards (~40-80 tokens/schema)",
    )
    methodology_resource: bool = Field(
        default=True, description="L3: Full methodology guide (lazy-loaded, 0 tokens)"
    )


class GuidanceConfig(BaseModel):
    """Configuration for code execution guidance system.

    This controls how much guidance PMCP provides to models about using
    code execution patterns. The default is "minimal" mode which provides
    basic guidance without bloating context.
    """

    level: Literal["off", "minimal", "standard"] = Field(
        default="minimal",
        description=(
            "Guidance level: "
            "'off' = no guidance, "
            "'minimal' = L0+L1 (~200 tokens), "
            "'standard' = L0+L1+L2 (~320 tokens)"
        ),
    )
    layers: GuidanceLayers = Field(default_factory=GuidanceLayers)
    max_hint_length: int = Field(
        default=8, description="Maximum characters for L1 code hints"
    )
    max_snippet_lines: int = Field(
        default=4, description="Maximum lines for L2 code snippets (if enabled)"
    )

    def __init__(self, **data):
        """Initialize guidance config and apply level presets."""
        super().__init__(**data)
        self._apply_level_preset()

    def _apply_level_preset(self):
        """Apply preset layer configurations based on level."""
        if self.level == "off":
            self.layers.mcp_instructions = False
            self.layers.code_hints = False
            self.layers.code_snippets = False
            self.layers.methodology_resource = False
        elif self.level == "minimal":
            self.layers.mcp_instructions = True
            self.layers.code_hints = True
            self.layers.code_snippets = False
            self.layers.methodology_resource = True
        elif self.level == "standard":
            self.layers.mcp_instructions = True
            self.layers.code_hints = True
            self.layers.code_snippets = True
            self.layers.methodology_resource = True

    @property
    def include_mcp_instructions(self) -> bool:
        """Whether to include L0 philosophy in MCP server instructions."""
        return self.layers.mcp_instructions

    @property
    def include_code_hints(self) -> bool:
        """Whether to include L1 hints in capability cards."""
        return self.layers.code_hints

    @property
    def include_code_snippets(self) -> bool:
        """Whether to include L2 code examples in schema cards."""
        return self.layers.code_snippets

    @property
    def include_methodology_resource(self) -> bool:
        """Whether to register L3 methodology guide as a resource."""
        return self.layers.methodology_resource

    def estimated_token_cost(
        self, num_search_results: int = 15, num_describes: int = 1
    ) -> int:
        """Estimate total token overhead for guidance in a typical workflow.

        Args:
            num_search_results: Number of capability cards returned in search (default: 15)
            num_describes: Number of describe calls made (default: 1)

        Returns:
            Estimated total tokens consumed by guidance
        """
        total = 0

        if self.include_mcp_instructions:
            total += 30  # ~25-35 tokens for L0 philosophy

        if self.include_code_hints:
            total += num_search_results * 10  # ~8-12 tokens per card

        if self.include_code_snippets:
            total += num_describes * 60  # ~40-80 tokens per schema

        # L3 resource is lazy-loaded, so 0 tokens unless explicitly requested

        return total


def load_guidance_config(config_path: Path | None = None) -> GuidanceConfig:
    """Load guidance configuration from YAML file.

    Args:
        config_path: Path to guidance config file. If None, looks for
                     ~/.claude/gateway-guidance.yaml

    Returns:
        GuidanceConfig instance with loaded or default settings
    """
    if config_path is None:
        config_path = Path.home() / ".claude" / "gateway-guidance.yaml"

    if not config_path.exists():
        # Return default config (minimal mode)
        return GuidanceConfig()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)

        if not data or "guidance" not in data:
            return GuidanceConfig()

        return GuidanceConfig(**data["guidance"])
    except Exception as e:
        # If config is invalid, log warning and use defaults
        print(f"Warning: Failed to load guidance config from {config_path}: {e}")
        print("Using default guidance config (minimal mode)")
        return GuidanceConfig()


def create_default_guidance_config(output_path: Path | None = None) -> Path:
    """Create a default guidance configuration file.

    Args:
        output_path: Where to write the config. If None, writes to
                     ~/.claude/gateway-guidance.yaml

    Returns:
        Path where the config was written
    """
    if output_path is None:
        output_path = Path.home() / ".claude" / "gateway-guidance.yaml"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    default_config = {
        "guidance": {
            "level": "minimal",
            "layers": {
                "mcp_instructions": True,
                "code_hints": True,
                "code_snippets": False,
                "methodology_resource": True,
            },
            "max_hint_length": 8,
            "max_snippet_lines": 4,
        }
    }

    with open(output_path, "w") as f:
        yaml.dump(default_config, f, default_flow_style=False, sort_keys=False)

    return output_path

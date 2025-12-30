"""Policy Layer - Handles allow/deny lists, output caps, and secret redaction."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from pmcp.types import GatewayPolicy

logger = logging.getLogger(__name__)

DEFAULT_REDACTION_PATTERNS = [
    # Common secret patterns (case-insensitive)
    r"(api[_-]?key|apikey)[\s]*[:=][\s]*[\"']?([^\s\"']+)",
    r"(secret|password|passwd|pwd)[\s]*[:=][\s]*[\"']?([^\s\"']+)",
    r"(bearer|token)[\s]+[a-zA-Z0-9._-]+",
    r"(aws_secret|aws_access)[\s]*[:=][\s]*[\"']?([^\s\"']+)",
]

DEFAULT_POLICY_PATHS = [
    Path.cwd() / ".mcp-gateway-policy.yaml",
    Path.cwd() / ".mcp-gateway-policy.json",
    Path.home() / ".claude" / "gateway-policy.yaml",
    Path.home() / ".claude" / "gateway-policy.json",
]


class PolicyManager:
    """Manages gateway policy including allow/deny lists, limits, and redaction."""

    def __init__(self, policy_path: Path | None = None) -> None:
        self._policy = GatewayPolicy()
        self._redaction_regexes: list[re.Pattern[str]] = []

        if policy_path:
            self._load_policy(policy_path)
        else:
            # Try default locations
            for default_path in DEFAULT_POLICY_PATHS:
                if default_path.exists():
                    self._load_policy(default_path)
                    break

        self._compile_redaction_patterns()

    def _load_policy(self, policy_path: Path) -> None:
        """Load policy from file."""
        try:
            content = policy_path.read_text()

            if policy_path.suffix in (".yaml", ".yml"):
                data = yaml.safe_load(content)
            else:
                data = json.loads(content)

            if data:
                self._policy = GatewayPolicy.model_validate(data)
            logger.info(f"Loaded policy from {policy_path}")
        except Exception as e:
            logger.warning(f"Failed to load policy from {policy_path}: {e}")

    def _compile_redaction_patterns(self) -> None:
        """Compile redaction regex patterns."""
        self._redaction_regexes = []

        # Use default patterns if none specified
        patterns = self._policy.redaction.patterns or DEFAULT_REDACTION_PATTERNS

        for pattern in patterns:
            try:
                self._redaction_regexes.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"Invalid redaction pattern '{pattern}': {e}")

    def _matches_any(self, value: str, patterns: list[str]) -> bool:
        """Check if value matches any glob pattern."""
        return any(fnmatch.fnmatch(value.lower(), p.lower()) for p in patterns)

    def is_server_allowed(self, server_name: str) -> bool:
        """Check if server is allowed by policy."""
        denylist = self._policy.servers.denylist
        allowlist = self._policy.servers.allowlist

        # Check denylist first
        if denylist and self._matches_any(server_name, denylist):
            return False

        # If allowlist is specified, server must be in it
        if allowlist:
            return self._matches_any(server_name, allowlist)

        return True

    def is_tool_allowed(self, tool_id: str) -> bool:
        """Check if tool is allowed by policy."""
        denylist = self._policy.tools.denylist
        allowlist = self._policy.tools.allowlist

        # Check denylist first
        if denylist and self._matches_any(tool_id, denylist):
            return False

        # If allowlist is specified, tool must be in it
        if allowlist:
            return self._matches_any(tool_id, allowlist)

        return True

    def is_resource_allowed(self, resource_id: str) -> bool:
        """Check if resource is allowed by policy.

        Args:
            resource_id: Resource ID in format "server_name::uri"
        """
        denylist = self._policy.resources.denylist
        allowlist = self._policy.resources.allowlist

        # Check denylist first
        if denylist and self._matches_any(resource_id, denylist):
            return False

        # If allowlist is specified, resource must be in it
        if allowlist:
            return self._matches_any(resource_id, allowlist)

        return True

    def is_prompt_allowed(self, prompt_id: str) -> bool:
        """Check if prompt is allowed by policy.

        Args:
            prompt_id: Prompt ID in format "server_name::name"
        """
        denylist = self._policy.prompts.denylist
        allowlist = self._policy.prompts.allowlist

        # Check denylist first
        if denylist and self._matches_any(prompt_id, denylist):
            return False

        # If allowlist is specified, prompt must be in it
        if allowlist:
            return self._matches_any(prompt_id, allowlist)

        return True

    def get_max_tools_per_server(self) -> int:
        """Get max tools per server limit."""
        return self._policy.limits.max_tools_per_server

    def get_max_output_bytes(self) -> int:
        """Get max output bytes."""
        return self._policy.limits.max_output_bytes

    def get_max_output_tokens(self) -> int:
        """Get max output tokens (rough estimate)."""
        return self._policy.limits.max_output_tokens

    def truncate_output(
        self, output: str, max_bytes: int | None = None
    ) -> tuple[str, bool, int]:
        """
        Truncate output to max size.

        Returns: (result, truncated, original_size)
        """
        max_size = max_bytes or self.get_max_output_bytes()
        original_size = len(output.encode("utf-8"))

        if original_size <= max_size:
            return (output, False, original_size)

        # Truncate to max bytes, being careful with UTF-8
        encoded = output.encode("utf-8")
        truncated_bytes = encoded[: max_size - 100]  # Leave room for message

        # Decode, ignoring incomplete characters at the end
        truncated_str = truncated_bytes.decode("utf-8", errors="ignore")

        # Add truncation indicator
        truncated_str += (
            f"\n\n[... OUTPUT TRUNCATED: {original_size} bytes -> {max_size} bytes ...]"
        )

        return (truncated_str, True, original_size)

    def redact_secrets(self, output: str) -> str:
        """Redact secrets from output."""
        result = output

        for regex in self._redaction_regexes:

            def replace_match(match: re.Match[str]) -> str:
                full_match = match.group(0)
                # Find the separator (: or =)
                for i, char in enumerate(full_match):
                    if char in ":=":
                        return full_match[: i + 1] + " [REDACTED]"
                return "[REDACTED]"

            result = regex.sub(replace_match, result)

        return result

    def process_output(
        self,
        output: Any,
        *,
        redact: bool = False,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """
        Process output: truncate and optionally redact.

        Returns dict with: result, truncated, raw_size, summary
        """
        # Convert to string
        if isinstance(output, str):
            output_str = output
        else:
            output_str = json.dumps(output, indent=2)

        raw_size = len(output_str.encode("utf-8"))

        # Truncate first
        truncated_str, truncated, _ = self.truncate_output(output_str, max_bytes)

        # Redact if requested
        final_str = self.redact_secrets(truncated_str) if redact else truncated_str

        # Generate summary if truncated
        summary: str | None = None
        if truncated:
            lines = output_str.count("\n") + 1
            first_line = output_str.split("\n")[0][:100] if output_str else ""
            summary = f'Output was {raw_size} bytes ({lines} lines). First line: "{first_line}..."'

        # Try to parse back to object if original was not string
        result: Any = final_str
        if not isinstance(output, str):
            try:
                result = json.loads(final_str)
            except json.JSONDecodeError:
                # Keep as string if truncation broke JSON
                pass

        return {
            "result": result,
            "truncated": truncated,
            "raw_size": raw_size,
            "summary": summary,
        }

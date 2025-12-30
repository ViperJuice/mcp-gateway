"""Tests for policy manager."""

from __future__ import annotations

import json
from pathlib import Path


from pmcp.policy.policy import PolicyManager


class TestServerAllowDeny:
    """Tests for server allow/deny lists."""

    def test_allows_all_by_default(self) -> None:
        policy = PolicyManager()
        assert policy.is_server_allowed("any-server") is True
        assert policy.is_server_allowed("another-server") is True

    def test_denies_servers_on_denylist(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "servers": {
                        "denylist": ["blocked-*", "dangerous"],
                    }
                }
            )
        )

        policy = PolicyManager(policy_path)
        assert policy.is_server_allowed("blocked-server") is False
        assert policy.is_server_allowed("blocked-anything") is False
        assert policy.is_server_allowed("dangerous") is False
        assert policy.is_server_allowed("allowed-server") is True

    def test_only_allows_servers_on_allowlist(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "servers": {
                        "allowlist": ["github", "jira"],
                    }
                }
            )
        )

        policy = PolicyManager(policy_path)
        assert policy.is_server_allowed("github") is True
        assert policy.is_server_allowed("jira") is True
        assert policy.is_server_allowed("slack") is False


class TestToolAllowDeny:
    """Tests for tool allow/deny lists."""

    def test_allows_all_by_default(self) -> None:
        policy = PolicyManager()
        assert policy.is_tool_allowed("github::create_issue") is True

    def test_supports_glob_patterns(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "tools": {
                        "denylist": ["*::delete_*", "dangerous::*"],
                    }
                }
            )
        )

        policy = PolicyManager(policy_path)
        assert policy.is_tool_allowed("github::delete_repo") is False
        assert policy.is_tool_allowed("jira::delete_issue") is False
        assert policy.is_tool_allowed("dangerous::anything") is False
        assert policy.is_tool_allowed("github::create_issue") is True


class TestResourceAllowDeny:
    """Tests for resource allow/deny lists."""

    def test_allows_all_by_default(self) -> None:
        policy = PolicyManager()
        assert policy.is_resource_allowed("github::file://readme.md") is True
        assert policy.is_resource_allowed("jira::jira://issue/123") is True

    def test_denies_resources_on_denylist(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "resources": {
                        "denylist": ["*::file://*.env", "secrets::*"],
                    }
                }
            )
        )

        policy = PolicyManager(policy_path)
        assert policy.is_resource_allowed("github::file://.env") is False
        assert policy.is_resource_allowed("any::file://config.env") is False
        assert policy.is_resource_allowed("secrets::anything") is False
        assert policy.is_resource_allowed("github::file://readme.md") is True

    def test_only_allows_resources_on_allowlist(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "resources": {
                        "allowlist": ["docs::*", "public::*"],
                    }
                }
            )
        )

        policy = PolicyManager(policy_path)
        assert policy.is_resource_allowed("docs::file://readme.md") is True
        assert policy.is_resource_allowed("public::https://example.com") is True
        assert policy.is_resource_allowed("private::file://secret.txt") is False


class TestPromptAllowDeny:
    """Tests for prompt allow/deny lists."""

    def test_allows_all_by_default(self) -> None:
        policy = PolicyManager()
        assert policy.is_prompt_allowed("github::create_issue") is True
        assert policy.is_prompt_allowed("jira::summarize") is True

    def test_denies_prompts_on_denylist(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "prompts": {
                        "denylist": ["*::dangerous_*", "admin::*"],
                    }
                }
            )
        )

        policy = PolicyManager(policy_path)
        assert policy.is_prompt_allowed("github::dangerous_action") is False
        assert policy.is_prompt_allowed("jira::dangerous_prompt") is False
        assert policy.is_prompt_allowed("admin::anything") is False
        assert policy.is_prompt_allowed("github::create_issue") is True

    def test_only_allows_prompts_on_allowlist(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "prompts": {
                        "allowlist": ["safe::*", "approved::*"],
                    }
                }
            )
        )

        policy = PolicyManager(policy_path)
        assert policy.is_prompt_allowed("safe::any_prompt") is True
        assert policy.is_prompt_allowed("approved::summarize") is True
        assert policy.is_prompt_allowed("unapproved::something") is False


class TestOutputTruncation:
    """Tests for output truncation."""

    def test_does_not_truncate_small_outputs(self) -> None:
        policy = PolicyManager()
        result, truncated, original_size = policy.truncate_output("short output")

        assert result == "short output"
        assert truncated is False
        assert original_size == 12

    def test_truncates_large_outputs(self) -> None:
        policy = PolicyManager()
        large_output = "x" * 100000
        result, truncated, original_size = policy.truncate_output(large_output, 1000)

        assert len(result) < 1000
        assert truncated is True
        assert original_size == 100000
        assert "[... OUTPUT TRUNCATED" in result


class TestSecretRedaction:
    """Tests for secret redaction."""

    def test_redacts_common_patterns(self) -> None:
        policy = PolicyManager()

        input_text = """
            API_KEY=sk-1234567890
            password: mysecretpassword
            Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9
        """

        redacted = policy.redact_secrets(input_text)

        assert "sk-1234567890" not in redacted
        assert "mysecretpassword" not in redacted
        assert "[REDACTED]" in redacted


class TestYamlPolicyLoading:
    """Tests for YAML policy loading."""

    def test_loads_yaml_policy(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "policy.yaml"
        policy_path.write_text(
            """
servers:
  denylist:
    - blocked-server
limits:
  max_output_bytes: 10000
"""
        )

        policy = PolicyManager(policy_path)
        assert policy.is_server_allowed("blocked-server") is False
        assert policy.get_max_output_bytes() == 10000

"""Gateway Tool Implementations."""

from __future__ import annotations

import logging
import os
import re
import json
import asyncio
import time
import platform
import shutil
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast
from urllib.request import urlopen
from urllib.parse import urlencode

from dotenv import load_dotenv
from mcp.types import Tool
from pmcp import __version__ as PMCP_VERSION
from pmcp.auth import (
    UNVERIFIED_URL_CAVEAT,
    is_verified_public_auth_url,
    normalize_auth_metadata,
    parse_url_elicitation_error,
    parse_www_authenticate,
    sanitize_auth_diagnostic,
    sanitize_url_elicitation_url,
)

from pmcp.client.manager import ClientManager, _terminate_process_tree
from pmcp.config.guidance import GuidanceConfig
from pmcp.config.loader import (
    registry_allow_private_from_config,
    StartupObservationSnapshot,
    StartupSkipReason,
    build_startup_observation_snapshot,
    get_startup_policy,
    is_legacy_manifest_auto_start_enabled,
    load_config_sources,
    load_configs,
    load_disabled_auto_start,
    load_enabled_auto_start,
    manifest_server_to_config,
    resolve_startup_configs,
    set_startup_policy,
    summarize_startup_resolution,
)
from pmcp.errors import ErrorCode, GatewayException, make_error
from pmcp.env_store import (
    record_dotenv_keys,
    sanitized_subprocess_env,
    set_env_value,
)
from pmcp.validation import env_var_allowed, is_valid_package_name
from pmcp.identity import filter_self_references
from pmcp.manifest.code_patterns_loader import get_code_hint
from pmcp.manifest.environment import CLIInfo, detect_platform, probe_clis
from pmcp.templates.code_snippets_loader import get_code_snippet
from pmcp.manifest.installer import (
    MissingApiKeyError,
    get_job_manager,
    InstallError,
)
from pmcp.manifest.loader import load_manifest
from pmcp.manifest.matcher import (
    _keyword_match_score,
    _manifest_keyword_weights,
    rank_cli_hints,
)
from pmcp.manifest.registry import (
    DEFAULT_REGISTRY_ENDPOINT,
    RegistryCache,
    RegistryServerEntry,
    effective_registry_endpoint,
    fetch_registry_servers,
    load_registry_cache,
)
from pmcp.manifest.refresher import save_descriptions_cache
from pmcp.manifest.npm_resolver import get_resolver
from pmcp.manifest.version_checker import (
    _docker_image_arg,
    _docker_image_digest,
    _docker_image_tag,
    _npm_package_arg,
    _npm_tag,
    _uvx_package_arg,
    detect_package_type,
    get_package_version,
)
from pmcp.policy.policy import PolicyManager
from pmcp.remote_auth import (
    MissingRemoteHeaderAuthError,
    build_remote_header_env_lookup,
    resolve_remote_headers,
)
from pmcp.types import (
    ArgInfo,
    AuthConnectInput,
    AuthConnectOutput,
    AuthEventKind,
    CancelInput,
    CancelOutput,
    CapabilityCandidate,
    CapabilityCard,
    CapabilityRequestInput,
    CapabilityResolution,
    CatalogSearchInput,
    CatalogSearchOutput,
    CLIResolution,
    ConfigStatusOutput,
    DescribeInput,
    DescriptionsCache,
    EffectiveConfigEntry,
    GatewayAuditEvent,
    GatewayDiagnosticsInfo,
    HealthOutput,
    InvokeInput,
    InvokeOutput,
    InvokeTemplate,
    ConnectServerInput,
    DisconnectServerInput,
    RestartServerInput,
    LifecycleServerOutput,
    ListPendingInput,
    ListPendingOutput,
    PendingRequestInfo,
    PrebuiltToolInfo,
    ProvisionInput,
    ProvisionJobStatus,
    ProvisionOutput,
    ProvisionStatusInput,
    RefreshInput,
    RefreshOutput,
    RegisterDiscoveredServerInput,
    RegisterDiscoveredServerOutput,
    RiskHint,
    SchemaCard,
    SearchRegistryInput,
    SearchRegistryOutput,
    SearchRegistryResult,
    ServerHealthInfo,
    StartupPolicyOperation,
    StartupPolicyOutput,
    StartupPolicyPreview,
    SyncEnvironmentInput,
    SyncEnvironmentOutput,
    SubmitFeedbackInput,
    SubmitFeedbackOutput,
    TasksCancelInput,
    TasksCancelOutput,
    TasksGetInput,
    TasksGetOutput,
    TasksListInput,
    TasksListOutput,
    TasksResultInput,
    TasksResultOutput,
    ToolInfo,
    TraceContextInfo,
    UpdateServerInput,
    UpdateServerOutput,
    LocalMcpServerConfig,
    McpTaskInfo,
    RemoteMcpServerConfig,
    ResolvedServerConfig,
    UrlElicitationInfo,
)

from pmcp.manifest.loader import (
    Manifest,
    ServerConfig,
    credential_lookup_keys,
    credential_storage_key,
    is_usable_credential_value,
    requires_credential,
)

logger = logging.getLogger(__name__)

FEEDBACK_TOKEN_LIMIT = 4000


def _refresh_config_unchanged(
    old: ResolvedServerConfig,
    new: ResolvedServerConfig,
    *,
    header_env_lookup: Callable[[str], str | None] | None = None,
    old_resolved_headers: dict[str, str] | None = None,
) -> bool:
    """Return True if two resolved configs describe the same downstream process.

    Used by gateway.refresh's diff so unchanged servers are left running.

    We compare only the fields that actually affect the spawned process
    (command/args/cwd/env for local, url/headers/transport for remote) rather
    than using full-model equality, for two reasons:

    * ``env`` is compared by its *effective* override only — entries that
      actually differ from ``os.environ``. Both the loader and the connect path
      now resolve a manifest server's credential the same way
      (``_manifest_server_to_config(..., os.environ.get)``), so the same logical
      server yields an identical ``env`` regardless of how it entered the manager
      and the effective-override comparison matches. (Before per-server secret
      namespacing, the loader used ``env=None`` and relied on ``_connect_stdio``
      seeding ``os.environ.copy()`` to supply the runtime credential; that broke
      once the credential moved to a namespaced storage key, so the loader now
      injects the resolved runtime ``env_var`` explicitly — do not revert it to
      ``lambda: None``.) Naively comparing full ``env`` would spuriously tear
      down running provisioned servers on every refresh (issue #79); comparing
      only the genuine override still detects a real env change. (Known
      limitation: rotation of an *ambient* secret that is not an explicit
      override cannot be detected here without a spawn-time env snapshot.)
    * ``source`` is excluded (e.g. ``manifest`` vs the configured source for the
      same effective server).

    Remote ``headers`` are compared *after* resolving ``${VAR}`` placeholders.
    The raw config keeps the same placeholder across a token rotation, so the
    only way to detect rotation is to compare the value the server was actually
    connected with — ``old_resolved_headers`` (captured at connect time) — against
    the freshly-resolved ``new`` headers (via ``header_env_lookup``). Without that
    the remote connection would silently keep stale/revoked auth across a refresh.
    When neither is supplied (e.g. unit tests), the raw headers are compared.
    """
    if old.name != new.name:
        return False
    old_cfg, new_cfg = old.config, new.config
    if type(old_cfg) is not type(new_cfg):
        return False
    if isinstance(old_cfg, LocalMcpServerConfig) and isinstance(
        new_cfg, LocalMcpServerConfig
    ):

        def _effective_env(env: dict[str, str] | None) -> dict[str, str]:
            # Only overrides that actually change the spawned environment.
            return {
                key: value
                for key, value in (env or {}).items()
                if os.environ.get(key) != value
            }

        return (
            old_cfg.command == new_cfg.command
            and old_cfg.args == new_cfg.args
            and old_cfg.cwd == new_cfg.cwd
            and _effective_env(old_cfg.env) == _effective_env(new_cfg.env)
        )
    if isinstance(old_cfg, RemoteMcpServerConfig) and isinstance(
        new_cfg, RemoteMcpServerConfig
    ):
        old_headers: dict[str, str] | None
        new_headers: dict[str, str] | None
        if header_env_lookup is not None:
            new_headers = resolve_remote_headers(
                new_cfg.headers, header_env_lookup
            ).resolved_headers
        else:
            new_headers = new_cfg.headers
        if old_resolved_headers is not None:
            # The value the running server was actually connected with — this is
            # what reveals an env-store token rotation.
            old_headers = old_resolved_headers
        elif header_env_lookup is not None:
            old_headers = resolve_remote_headers(
                old_cfg.headers, header_env_lookup
            ).resolved_headers
        else:
            old_headers = old_cfg.headers
        return (
            old_cfg.url == new_cfg.url
            and old_headers == new_headers
            and old_cfg.type == new_cfg.type
        )
    return False


def _detect_effective_version_pin(
    package_type: Literal["npm", "pypi", "cargo", "docker", "unknown"],
    command: str,
    args: list[str],
    env: Mapping[str, str] | None,
    cwd: str | None,
) -> str | None:
    """Return the pinned version/tag *args* explicitly locks to, or ``None``.

    *env* (the server's environment OVERLAY) and *cwd* are threaded through to
    ``_npm_package_arg`` and are REQUIRED for the same reason they are there: on
    the npm path they are identity inputs, and a defaulted ``None`` would let a
    caller that has a server's ``env``/``cwd`` in hand forget to pass them and
    still type-check.

    Used by gateway.update_server: a server pinned to a concrete version
    cannot be moved to ``@latest`` by this tool while the pin stands (the
    README documents pinning a server's version in ``.mcp.json`` as the
    supported configuration channel, e.g. for ``index-it-mcp``).

    npm: reuses ``_npm_package_arg``/``_strip_npm_tag`` (the exact scan
    ``detect_package_type`` itself uses) rather than re-scanning
    independently, so this can never disagree with detect_package_type about
    which arg is "the package". ``@latest`` is not a pin -- it's what
    gateway.update_server would install anyway.

    pypi/uvx: ``detect_package_type`` does not strip anything for uvx, so an
    inline ``pkg==1.2.3`` token already makes the later PyPI lookup fail
    (returning ``latest_version=None``, which the existing "don't write on a
    failed fetch" gate already protects against) -- this check exists only
    to turn that into a clear, specific refusal message instead of an opaque
    "could not fetch latest version" one.

    docker: ``detect_package_type`` strips the ``:tag`` off the image
    reference, so without this branch ``docker run acme/server:1.2.3`` would
    pull ``acme/server:latest``, restart the still-pinned ``1.2.3`` config,
    and then record the latest digest as active -- the same silent
    misreporting the npm branch above exists to prevent (board review round
    3). Uses ``_docker_image_arg``/``_docker_image_tag`` so a registry host
    with a port (``registry:5000/img``) is not misread as a tag.

    cargo: ``cargo install`` pins with a separate ``--version X`` flag rather
    than an inline suffix, so it needs its own scan.
    """
    if package_type == "npm":
        # `command` is passed for the same reason detect_package_type passes
        # it: the scan is shared so the two cannot disagree about which
        # argument is "the package", and it can only be right for both `npm`
        # (subcommand first) and `npx` (package first) if it knows which it is
        # reading. Before this, `npm exec pkg@1.2` scanned to `exec`, whose
        # `_npm_tag` is None, so a REAL pin was reported as unpinned.
        raw = _npm_package_arg(args, command, env, cwd)
        if raw is None:
            return None
        tag = _npm_tag(raw)
        return None if not tag or tag == "latest" else tag
    if package_type == "pypi" and command == "uvx":
        # Shares `_uvx_package_arg` with detect_package_type for the same
        # reason the npm branch shares `_npm_package_arg`: two independent
        # scans disagreeing about which token is "the package" is how a real
        # pin gets missed. The previous scan skipped every `-`-prefixed token
        # and read `==` off the first bare one, which was wrong twice over
        # (Consiliency/pmcp#182): `--from=pkg==1.2.0` is a single `-`-prefixed
        # token, so a REAL pin read as unpinned and update_server would have
        # probed for latest and moved a server the operator had pinned; and
        # `--with requests==2.0 pkg` reported an injected DEPENDENCY's version
        # as the server's own pin, refusing an update that was never pinned.
        raw, _from_flag = _uvx_package_arg(args)
        if raw is not None and "==" in raw:
            _, _, version = raw.partition("==")
            return version or None
    if package_type == "docker":
        raw_image = _docker_image_arg(args)
        if raw_image is None:
            return None
        # A DIGEST is the tightest pin docker has -- immutable content
        # identity, stronger than any tag -- so it must be checked first and
        # independently of the tag. `_docker_image_tag` deliberately returns
        # None for a digest-only reference (a digest is not a tag), so reading
        # the tag alone would report `img@sha256:...` as UNPINNED. update_server
        # would then pull `image:latest`, restart the unchanged digest-pinned
        # config, and record the registry's newest digest -- announcing an
        # update while still running the old immutable image (ah board review,
        # red-team seat). `image:latest@sha256:...` is the same trap wearing a
        # tag that the `== "latest"` test would otherwise discard.
        digest = _docker_image_digest(raw_image)
        if digest:
            return digest
        tag = _docker_image_tag(raw_image)
        return None if not tag or tag == "latest" else tag
    if package_type == "cargo":
        for index, arg in enumerate(args):
            if arg.startswith("--version="):
                _, _, version = arg.partition("=")
                return version or None
            if arg == "--version" and index + 1 < len(args):
                return args[index + 1] or None
    return None


# Human-readable label for a ResolvedServerConfig.source, used in messages
# that need to point an operator at the file a pin (or other override) came
# from.
_CONFIG_SOURCE_LABELS: dict[str, str] = {
    "project": "the project .mcp.json",
    "user": "the user .mcp.json",
    "custom": "the custom MCP config file",
    "manifest": "the manifest entry",
}


# Risk level ordering for filtering
RISK_ORDER = {"low": 1, "medium": 2, "high": 3, "unknown": 4}
TRACE_CONTEXT_KEYS = ("traceparent", "tracestate", "baggage")
TRACE_VALUE_DENY_PATTERN = re.compile(
    r"(bearer\s+|authorization|api[_-]?key|token|password|secret|sk-[A-Za-z0-9_-]{12,})",
    re.I,
)


def get_gateway_tool_definitions() -> list[Tool]:
    """Get MCP tool definitions for the gateway."""
    return [
        Tool(
            name="gateway.catalog_search",
            description=(
                "Search for available tools across all connected MCP servers. "
                "Returns compact capability cards without full schemas. "
                "Use filters to narrow results by server, tags, or risk level. "
                "Set include_offline=True to also discover provisionable servers not yet running. "
                "This is the primary tool discovery entry point."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to match against tool names, descriptions, and tags",
                    },
                    "filters": {
                        "type": "object",
                        "properties": {
                            "server": {
                                "type": "string",
                                "description": "Filter to tools from a specific server",
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Filter to tools with any of these tags",
                            },
                            "risk_max": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                                "description": "Maximum risk level to include",
                            },
                        },
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                        "description": "Maximum number of results to return",
                    },
                    "include_offline": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include tools from offline servers",
                    },
                },
            },
        ),
        Tool(
            name="gateway.describe",
            description=(
                "Get detailed information about a specific tool, including its arguments and constraints. "
                "Use this before invoking a tool to understand its requirements."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": 'The tool ID in format "server_name::tool_name"',
                    },
                },
                "required": ["tool_id"],
            },
        ),
        Tool(
            name="gateway.invoke",
            description=(
                "Invoke a tool on a downstream MCP server. "
                "Arguments are validated against the tool schema before execution. "
                "Output is automatically truncated if too large."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": 'The tool ID in format "server_name::tool_name"',
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments to pass to the tool (must match tool schema)",
                    },
                    "run_correlation_id": {
                        "type": "string",
                        "description": "Scoped-advisor run correlation ID",
                    },
                    "seat_correlation_id": {
                        "type": "string",
                        "description": "Scoped-advisor seat correlation ID",
                    },
                    "evidence_label_digest": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                        "description": "SHA-256 digest of the caller evidence label",
                    },
                    "options": {
                        "type": "object",
                        "properties": {
                            "timeout_ms": {
                                "type": "integer",
                                "minimum": 1000,
                                "maximum": 300000,
                                "default": 30000,
                                "description": "Timeout in milliseconds",
                            },
                            "max_output_chars": {
                                "type": "integer",
                                "minimum": 100,
                                "maximum": 100000,
                                "description": "Maximum output characters (truncated if exceeded)",
                            },
                            "redact_secrets": {
                                "type": "boolean",
                                "default": False,
                                "description": "Redact detected secrets from output",
                            },
                        },
                    },
                },
                "required": ["tool_id"],
            },
        ),
        Tool(
            name="gateway.refresh",
            description=(
                "Reload backend MCP server configurations and reconnect. "
                "Use this when new MCP servers have been configured or to recover from connection errors. "
                "Refuses by default while downstream requests are pending; set force=true to cancel them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["claude_config", "custom"],
                        "description": "Config source to reload from",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for refresh (for logging)",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Cancel pending downstream requests before refreshing",
                    },
                },
            },
        ),
        Tool(
            name="gateway.connect_server",
            description=(
                "Connect or start a known downstream MCP server by name. "
                "Resolves configured, provisioned manifest, and registered discovered servers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the server to connect",
                    },
                },
                "required": ["server_name"],
            },
        ),
        Tool(
            name="gateway.disconnect_server",
            description=(
                "Disconnect a running downstream MCP server without changing persistent config. "
                "Refuses by default when that server has pending requests; set force=true to cancel them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the server to disconnect",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Cancel this server's pending requests before disconnecting",
                    },
                },
                "required": ["server_name"],
            },
        ),
        Tool(
            name="gateway.restart_server",
            description=(
                "Restart a known downstream MCP server without changing persistent config. "
                "Refuses by default when that server has pending requests; set force=true to cancel them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the server to restart",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Cancel this server's pending requests before restarting",
                    },
                },
                "required": ["server_name"],
            },
        ),
        Tool(
            name="gateway.health",
            description=(
                "Get the health status of the gateway and all connected MCP servers. "
                "Shows server status, tool counts, and last refresh time."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="gateway.config_status",
            description=(
                "Show read-only effective configuration and startup policy status "
                "with source attribution and non-secret diagnostics."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        Tool(
            name="gateway.get_startup_policy",
            description=(
                "Return persisted autoStart and legacy disableAutoStart entries "
                "grouped by config source."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        Tool(
            name="gateway.set_startup_policy",
            description=(
                "Preview or explicitly apply an autoStart add/remove/set operation "
                "against one selected config source or path."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["add", "remove", "set"],
                    },
                    "names": {"type": "array", "items": {"type": "string"}},
                    "source": {
                        "type": "string",
                        "enum": ["project", "user", "custom"],
                    },
                    "path": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": True},
                    "apply": {"type": "boolean", "default": False},
                },
                "required": ["operation"],
            },
        ),
        Tool(
            name="gateway.request_capability",
            description=(
                "Recommend the right tool for a task — describe what you need in natural language. "
                "Examples: 'scrape a website', 'search Slack messages', 'query Postgres', 'browse the web'. "
                "Matches against installed CLIs and 90+ provisionable MCP servers and returns ranked candidates; "
                "it does NOT start anything — call gateway.provision to actually install/start the recommended server. "
                "Prefer this over gateway.provision when you don't already know the exact server name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of the capability needed (e.g., 'I need to scrape a website', 'browser automation')",
                    },
                    "available_clis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: CLIs known to be available in the environment",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="gateway.sync_environment",
            description=(
                "Sync environment information from the host. "
                "Detects the platform (mac/wsl/linux/windows) and probes for installed CLIs. "
                "This information is used to prefer CLIs over MCP servers when matching capabilities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["mac", "wsl", "linux", "windows"],
                        "description": "Override detected platform (optional)",
                    },
                    "detected_clis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Override detected CLIs (optional)",
                    },
                },
            },
        ),
        Tool(
            name="gateway.provision",
            description=(
                "Provision (install and start) a specific MCP server from the manifest. "
                "Use this after reviewing candidates from gateway.request_capability. "
                "Returns immediately with a job_id for tracking. "
                "Poll gateway.provision_status to check progress. "
                "Use gateway.request_capability instead if you don't know the exact server name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the server to provision (from manifest)",
                    },
                },
                "required": ["server_name"],
            },
        ),
        Tool(
            name="gateway.update_server",
            description=(
                "Update a subordinate MCP server package to latest version and restart it "
                "so the new version is actually running. "
                "Call this to check for and apply an update -- the gateway does not "
                "volunteer update notices, so nothing will prompt you. "
                "Refuses to restart by default when the server has pending requests; "
                "set force=true to cancel them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of server to update",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Cancel this server's pending requests before restarting",
                    },
                },
                "required": ["server_name"],
            },
        ),
        Tool(
            name="gateway.auth_connect",
            description=(
                "Store credentials for a server and make them available to provisioning. "
                "Use this when gateway.provision reports missing authentication."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Server name that needs authentication",
                    },
                    "credential": {
                        "type": "string",
                        "description": "API key, token, or subscription credential to store",
                    },
                    "auth_mode": {
                        "type": "string",
                        "enum": ["api_key", "url_elicitation"],
                        "default": "api_key",
                        "description": "API-key storage or URL-mode elicitation acknowledgement",
                    },
                    "elicitation_id": {
                        "type": "string",
                        "description": "URL-mode elicitation identifier",
                    },
                    "elicitation_url": {
                        "type": "string",
                        "description": "Sanitized URL-mode elicitation URL",
                    },
                    "consent_acknowledged": {
                        "type": "boolean",
                        "default": False,
                        "description": "Acknowledge that the out-of-band URL flow was completed",
                    },
                    "env_var": {
                        "type": "string",
                        "description": "Optional explicit environment variable key",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["user", "project"],
                        "default": "user",
                        "description": "Where to store the credential",
                    },
                },
                "required": ["server_name"],
            },
        ),
        Tool(
            name="gateway.submit_feedback",
            description=(
                "Prepare and optionally submit a PMCP feedback issue to GitHub. "
                "By default returns an exact preview payload; set confirm_submission=true to submit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Issue title",
                    },
                    "description": {
                        "type": "string",
                        "description": "Issue details (technical data only)",
                    },
                    "issue_type": {
                        "type": "string",
                        "enum": ["bug", "feature_request"],
                        "default": "bug",
                    },
                    "subordinate_server": {
                        "type": "string",
                        "description": "Subordinate MCP server involved (if known)",
                    },
                    "failed_tool_call": {
                        "type": "string",
                        "description": "Specific failed tool call (if known)",
                    },
                    "confirm_submission": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set true only after user confirms submission",
                    },
                },
                "required": ["title", "description"],
            },
        ),
        Tool(
            name="gateway.provision_status",
            description=(
                "Check the status of a running server installation. "
                "Use after gateway.provision returns a job_id. "
                "Returns progress percentage, output log, and final tools when complete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job ID from gateway.provision response",
                    },
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="gateway.list_pending",
            description=(
                "List all pending tool invocations with health status. "
                "Shows elapsed time, heartbeat age, and current state for each request. "
                "Use this to monitor long-running operations before deciding to cancel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Filter to pending requests on a specific server (optional)",
                    },
                },
            },
        ),
        Tool(
            name="gateway.cancel",
            description=(
                "Cancel a pending tool invocation. "
                "By default, refuses to cancel healthy requests (recent heartbeat). "
                "Use force=true to cancel anyway. "
                "Use gateway.list_pending first to see request IDs and health status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": 'Request ID in format "server_name::local_id" from gateway.list_pending',
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Force cancel even if request is healthy (has recent heartbeat)",
                    },
                },
                "required": ["request_id"],
            },
        ),
        Tool(
            name="gateway.tasks_list",
            description=(
                "List brokered downstream MCP tasks. "
                "MCP task IDs are opaque downstream task identifiers, not PMCP request IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Optional server filter",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Optional downstream pagination cursor",
                    },
                },
            },
        ),
        Tool(
            name="gateway.tasks_get",
            description="Get current status for one downstream MCP task.",
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {"type": "string"},
                    "task_id": {"type": "string"},
                },
                "required": ["server_name", "task_id"],
            },
        ),
        Tool(
            name="gateway.tasks_result",
            description=(
                "Fetch a downstream MCP task result and apply the same output "
                "redaction and truncation options as gateway.invoke."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {"type": "string"},
                    "task_id": {"type": "string"},
                    "options": {
                        "type": "object",
                        "properties": {
                            "max_output_chars": {"type": "integer"},
                            "redact_secrets": {"type": "boolean"},
                        },
                    },
                },
                "required": ["server_name", "task_id"],
            },
        ),
        Tool(
            name="gateway.tasks_cancel",
            description=(
                "Cancel a downstream MCP task by opaque task ID. "
                "Use gateway.cancel only for PMCP request IDs from gateway.list_pending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_name": {"type": "string"},
                    "task_id": {"type": "string"},
                    "force": {"type": "boolean", "default": False},
                },
                "required": ["server_name", "task_id"],
            },
        ),
        Tool(
            name="gateway.search_registry",
            description=(
                "Search the public MCP Registry for external servers not in the local manifest. "
                "Use this when gateway.request_capability returns not_available. "
                "Returns package names and metadata; call gateway.register_discovered_server then gateway.provision to install."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of the capability needed",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                        "description": "Maximum number of results to return",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="gateway.register_discovered_server",
            description=(
                "Register an externally-discovered MCP server package so it can be provisioned. "
                "Call this after gateway.search_registry to register the chosen package, "
                "then call gateway.provision to install and start it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "npm package identifier (e.g. '@modelcontextprotocol/server-github')",
                    },
                    "server_name": {
                        "type": "string",
                        "description": "Logical name for this server (e.g. 'github') used with gateway.provision",
                    },
                    "env_vars": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required environment variable names (e.g. ['GITHUB_TOKEN'])",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of the server's purpose",
                    },
                },
                "required": ["package", "server_name"],
            },
        ),
    ]


def _summarize_arg_schema(
    prop: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, str]:
    """Summarize one argument's JSON Schema, one level deep.

    Returns ``(type_str, item_schema, placeholder)`` where ``item_schema`` is a
    compact nested summary (or None for scalars) and ``placeholder`` is a JSON
    example string. For array-of-objects the placeholder shows the nested item
    shape (e.g. ``[{"query": "<string>"}]``) so agents can invoke correctly on
    the first try instead of guessing at the bare ``array`` type.
    """
    prop_type = str(prop.get("type", "unknown"))

    def _prop_type(p: dict[str, Any]) -> str:
        return str(p.get("type", "unknown"))

    if prop_type == "array":
        items = prop.get("items")
        if isinstance(items, dict):
            item_type = _prop_type(items)
            if item_type == "object":
                item_props = items.get("properties", {})
                item_required = items.get("required", [])
                properties = {
                    key: _prop_type(value) if isinstance(value, dict) else "unknown"
                    for key, value in item_props.items()
                }
                item_schema: dict[str, Any] = {
                    "type": "object",
                    "required": list(item_required),
                    "properties": properties,
                }
                # Prefer required item fields for the example; fall back to all.
                example_keys = list(item_required) or list(properties)
                example = {
                    key: f"<{properties.get(key, 'unknown')}>" for key in example_keys
                }
                return prop_type, item_schema, json.dumps([example])
            # Array of scalars: record item type only.
            return prop_type, {"type": item_type}, json.dumps([f"<{item_type}>"])
        return prop_type, None, "<required: array>"

    if prop_type == "object":
        obj_props = prop.get("properties")
        if isinstance(obj_props, dict):
            obj_required = prop.get("required", [])
            properties = {
                key: _prop_type(value) if isinstance(value, dict) else "unknown"
                for key, value in obj_props.items()
            }
            item_schema = {
                "required": list(obj_required),
                "properties": properties,
            }
            example = {key: f"<{value}>" for key, value in properties.items()}
            return prop_type, item_schema, json.dumps(example)
        return prop_type, None, "<required: object>"

    # Scalar: no nested schema, caller keeps the existing placeholder form.
    return prop_type, None, ""


class GatewayTools:
    """Gateway tool handler implementations."""

    def __init__(
        self,
        client_manager: ClientManager,
        policy_manager: PolicyManager,
        project_root: Path | None = None,
        custom_config_path: Path | None = None,
        guidance_config: GuidanceConfig | None = None,
        descriptions_cache: DescriptionsCache | None = None,
        descriptions_cache_path: Path | None = None,
    ) -> None:
        self._client_manager = client_manager
        self._policy_manager = policy_manager
        self._project_root = project_root
        self._custom_config_path = custom_config_path
        self._guidance_config = guidance_config
        self._descriptions_cache = descriptions_cache
        # Where the descriptions cache lives on disk (.mcp-gateway/descriptions.yaml
        # by default). Needed so a successful gateway.update_server can persist the
        # refreshed version, not just patch the in-memory copy -- see update_server().
        self._descriptions_cache_path = descriptions_cache_path
        self._detected_clis: set[str] | None = None
        self._detected_cli_infos: dict[str, CLIInfo] = {}
        self._platform: str | None = None
        self._discovered_server_configs: dict[str, ServerConfig] = {}
        # provision_status finalization is one-shot per job: the per-job lock
        # serializes the server_ready→adopt handoff (and the complete→refresh)
        # so concurrent polls cannot double-adopt or re-refresh a finished job.
        self._provision_finalize_locks: dict[str, asyncio.Lock] = {}
        self._provision_finalized: set[str] = set()
        self._feedback_events: list[dict[str, Any]] = []
        self._audit_events: deque[GatewayAuditEvent] = deque(maxlen=64)
        self._provisioned_registry: dict[str, str | None] = (
            self._load_provisioned_registry()
        )
        self._startup_observations: StartupObservationSnapshot = {}
        self._transport_diagnostics = GatewayDiagnosticsInfo(
            transport="gateway",
            audit_buffer_size=self._audit_events.maxlen or 0,
        )

    def _build_cli_probe_configs(self, manifest: Manifest) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "check_command": cli.check_command,
                "help_command": cli.help_command,
            }
            for name, cli in manifest.cli_alternatives.items()
        }

    async def _resolve_cli_availability(
        self,
        manifest: Manifest,
        *,
        explicit_available_clis: list[str] | None = None,
    ) -> tuple[set[str], dict[str, CLIInfo]]:
        detected_cli_infos = dict(self._detected_cli_infos)

        if explicit_available_clis is not None:
            detected_clis = set(explicit_available_clis)
            detected_clis.update(detected_cli_infos)
            if self._detected_clis is not None:
                detected_clis.update(self._detected_clis)
            return detected_clis, detected_cli_infos

        if detected_cli_infos:
            detected_clis = set(detected_cli_infos)
            self._detected_clis = detected_clis
            return detected_clis, detected_cli_infos

        if self._detected_clis is not None:
            return set(self._detected_clis), detected_cli_infos

        platform = detect_platform()
        detected_cli_infos = await probe_clis(self._build_cli_probe_configs(manifest))
        detected_clis = set(detected_cli_infos)
        self._detected_cli_infos = detected_cli_infos
        self._detected_clis = detected_clis
        self._platform = platform
        return detected_clis, detected_cli_infos

    def set_startup_observations(
        self, observations: StartupObservationSnapshot
    ) -> None:
        """Replace startup policy observations used by gateway.health."""
        self._startup_observations = dict(observations)

    def set_transport_diagnostics(self, diagnostics: GatewayDiagnosticsInfo) -> None:
        """Replace safe transport diagnostics surfaced by gateway.health."""
        self._transport_diagnostics = diagnostics

    def _safe_trace_value(self, value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        if len(value) > 1024 or TRACE_VALUE_DENY_PATTERN.search(value):
            return None
        return value

    def _extract_trace_context(
        self, input_data: dict[str, Any]
    ) -> TraceContextInfo | None:
        values: dict[str, str] = {}
        for candidate in (
            input_data.get("_meta"),
            input_data.get("meta"),
            input_data.get("trace_context"),
            input_data.get("traceContext"),
        ):
            if not isinstance(candidate, dict):
                continue
            for key in TRACE_CONTEXT_KEYS:
                if key in values:
                    continue
                safe_value = self._safe_trace_value(candidate.get(key))
                if safe_value is not None:
                    values[key] = safe_value
        if not values:
            return None
        return TraceContextInfo.model_validate(values)

    def _audit(
        self,
        *,
        method: str,
        action: str,
        outcome: Literal["success", "failure", "refused"],
        started_at: float,
        server_name: str | None = None,
        tool_id: str | None = None,
        task_id: str | None = None,
        protocol_version: str | None = None,
        auth_state: str = "none",
        auth_event: AuthEventKind | None = None,
        error: str | None = None,
        trace_context: TraceContextInfo | None = None,
    ) -> None:
        safe_error = None
        if error:
            safe_error = self._sanitize_error(Exception(error))
        if auth_event is None:
            auth_event = self._auth_event_for_state(auth_state)
        self._audit_events.append(
            GatewayAuditEvent(
                timestamp=time.time(),
                method=method,
                action=action,
                outcome=outcome,
                latency_ms=round((time.monotonic() - started_at) * 1000),
                server_name=server_name,
                tool_id=tool_id,
                task_id=task_id,
                protocol_version=protocol_version,
                auth_state=cast(Any, auth_state),
                auth_event=auth_event,
                error=safe_error,
                trace_present=trace_context is not None,
            )
        )

    @staticmethod
    def _sanitize_error(e: Exception) -> str:
        """Return a safe error string: strip absolute paths, truncate to 400 chars."""
        msg = sanitize_auth_diagnostic(e)
        msg = re.sub(r"(/[^\s:,\"']+)", lambda m: os.path.basename(m.group(1)), msg)
        return msg[:400]

    def _auth_metadata_for_server(self, server_config: ServerConfig | None):
        if server_config is None:
            return None
        metadata = normalize_auth_metadata(
            protected_resource_metadata_url=server_config.protected_resource_metadata_url,
            authorization_server_metadata_url=server_config.authorization_server_metadata_url,
            oidc_issuer_url=server_config.oidc_issuer_url,
            oidc_discovery_url=server_config.oidc_discovery_url,
            client_id_metadata_document_url=server_config.client_id_metadata_document_url,
            declared_scopes=server_config.declared_scopes,
        )
        if not any(
            [
                metadata.protected_resource_metadata_url,
                metadata.authorization_server_metadata_url,
                metadata.oidc_issuer_url,
                metadata.oidc_discovery_url,
                metadata.client_id_metadata_document_url,
                metadata.declared_scopes,
            ]
        ):
            return None
        return metadata

    def _auth_challenge_from_message(self, message: str):
        match = re.search(r"WWW-Authenticate(?:\s*[:=]\s*|\s+)(.+)", message, re.I)
        if not match:
            return None
        return parse_www_authenticate(match.group(1))

    @staticmethod
    def _auth_event_for_state(auth_state: str) -> AuthEventKind | None:
        if auth_state == "insufficient_scope":
            return "insufficient_scope"
        if auth_state == "elicitation_required":
            return "url_elicitation_required"
        if auth_state == "policy_denied":
            return "policy_denied"
        return None

    @staticmethod
    def _auth_event_for_challenge(auth_challenge: Any | None) -> AuthEventKind | None:
        if auth_challenge is None:
            return None
        if getattr(auth_challenge, "missing_scopes", None):
            return "insufficient_scope"
        return "remote_auth_challenge"

    @property
    def _provisioned_registry_path(self) -> Path:
        return Path.home() / ".config" / "pmcp" / "provisioned.json"

    def _load_provisioned_registry(self) -> dict[str, str | None]:
        """Load the persisted provisioned-server registry from disk."""
        path = self._provisioned_registry_path
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(k, str)}
        except Exception as e:
            logger.warning(f"Could not load provisioned registry: {e}")
            return {}

    def _save_provisioned_registry(self) -> None:
        """Persist the provisioned-server registry to disk."""
        path = self._provisioned_registry_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._provisioned_registry, f)
        except Exception as e:
            logger.warning(f"Could not save provisioned registry: {e}")

    def _register_provisioned_server(
        self, server_name: str, env_var: str | None
    ) -> None:
        """Record a successfully provisioned server in the registry."""
        self._provisioned_registry[server_name] = env_var
        self._save_provisioned_registry()

    # === Stale Version Indexer ===

    async def _ensure_server_for_tool(self, tool_id: str) -> bool:
        """Ensure server is connected for a tool, triggering lazy-start if needed.

        Args:
            tool_id: Tool ID in format "server::tool_name"

        Returns:
            True if server is ready, False if connection failed
        """
        # Parse server name from tool_id
        if "::" not in tool_id:
            return False
        server_name = tool_id.split("::")[0]

        # Check if server needs lazy-start
        if self._client_manager.is_lazy_server(server_name):
            logger.info(f"Triggering lazy-start for server: {server_name}")
            return await self._client_manager.ensure_connected(server_name)

        # Server is either online or in error state
        return self._client_manager.is_server_online(server_name)

    def _get_cached_tools_for_offline_servers(self) -> list[ToolInfo]:
        """Get tool info from cache for servers that are not currently online.

        Returns ToolInfo objects for offline servers so they can be
        included in catalog_search results when include_offline=True.
        """
        if not self._descriptions_cache:
            return []

        cached_tools: list[ToolInfo] = []

        for server_name, server_desc in sorted(
            self._descriptions_cache.servers.items()
        ):
            # Skip servers that are already online (live tools take precedence)
            if self._client_manager.is_server_online(server_name):
                continue

            # Check policy allows this server
            if not self._policy_manager.is_server_allowed(server_name):
                continue

            # Convert cached PrebuiltToolInfo to ToolInfo
            for tool in sorted(server_desc.tools, key=lambda item: item.name):
                tool_id = f"{server_name}::{tool.name}"

                # Check policy allows this tool
                if not self._policy_manager.is_tool_allowed(tool_id):
                    continue

                # Convert risk_hint string to RiskHint enum
                risk = RiskHint.UNKNOWN
                if tool.risk_hint == "low":
                    risk = RiskHint.LOW
                elif tool.risk_hint == "medium":
                    risk = RiskHint.MEDIUM
                elif tool.risk_hint == "high":
                    risk = RiskHint.HIGH

                cached_tools.append(
                    ToolInfo(
                        tool_id=tool_id,
                        server_name=server_name,
                        tool_name=tool.name,
                        description=tool.description,
                        short_description=tool.short_description,
                        tags=tool.tags,
                        risk_hint=risk,
                        input_schema={},  # Not available in cache
                    )
                )

        return cached_tools

    def _manifest_candidates_for_query(
        self,
        query: str,
        *,
        manifest: Manifest,
        configured_servers: dict[str, ResolvedServerConfig],
        exclude_servers: set[str],
        limit: int = 5,
    ) -> list[CapabilityCandidate]:
        """Match a query against manifest servers and emit provision candidates.

        Surfaces manifest-only servers (never started, so no cached tools are
        represented in catalog_search) so an agent can provision the exact
        server instead of falling back to a plain web search (issue #78).
        Scoring reuses the manifest keyword matcher; a normalized name match is
        also accepted so servers that don't list their own name still surface.
        """
        query = (query or "").strip()
        if not query:
            return []

        query_words = query.lower().replace("-", " ").replace("_", " ").split()
        keyword_weights = _manifest_keyword_weights(manifest)

        scored: list[tuple[float, str, ServerConfig]] = []
        for name, server in manifest.servers.items():
            if name in exclude_servers:
                continue
            if not self._policy_manager.is_server_allowed(name):
                continue

            score = _keyword_match_score(query, server.keywords, keyword_weights)

            # Normalized name match (e.g. "bright data" -> "brightdata").
            norm_name = name.lower().replace("-", "").replace("_", "")
            for window_size in (3, 2, 1):
                for i in range(len(query_words) - window_size + 1):
                    window = "".join(query_words[i : i + window_size])
                    if window == norm_name:
                        score = max(score, 1.0)
                        break

            if score >= 0.2:  # Same minimum threshold as the matcher
                scored.append((score, name, server))

        scored.sort(key=lambda item: (-item[0], item[1]))

        running_servers = {
            s.name
            for s in self._client_manager.get_all_server_statuses()
            if s.status.value == "online"
        }

        candidates: list[CapabilityCandidate] = []
        for score, name, server in scored[:limit]:
            requires_api_key, env_var, env_instructions = self._get_server_env_metadata(
                name, manifest, configured_servers
            )
            candidates.append(
                CapabilityCandidate(
                    name=name,
                    candidate_type="server",
                    relevance_score=min(1.0, score),
                    reasoning=server.description,
                    requires_api_key=requires_api_key,
                    api_key_available=self._check_any_api_key_available(
                        self._auth_env_options(name, env_var)
                    ),
                    env_var=env_var,
                    env_instructions=env_instructions,
                    is_running=name in running_servers,
                    source="manifest",
                    transport=server.transport,
                    url=server.url,
                    package=server.package,
                    server_card_url=server.server_card_url,
                    declared_scopes=server.declared_scopes,
                    declared_capabilities=server.declared_capabilities,
                    provisionable=True,
                    provision_tool="gateway.provision",
                    request_capability_tool="gateway.request_capability",
                    auth_tool="gateway.auth_connect",
                )
            )
        return candidates

    async def catalog_search(self, input_data: dict[str, Any]) -> CatalogSearchOutput:
        """gateway.catalog_search - Search for available tools."""
        parsed = CatalogSearchInput.model_validate(input_data)
        cli_hints = []
        scoped_advisor = self._policy_manager.scoped_advisor_active is True
        query = parsed.query.strip() if parsed.query else ""
        if query and not scoped_advisor:
            manifest = load_manifest()
            detected_clis, detected_cli_infos = await self._resolve_cli_availability(
                manifest
            )
            cli_hints = [
                match.hint
                for match in rank_cli_hints(
                    query,
                    manifest,
                    available_clis=detected_clis,
                    detected_cli_infos=detected_cli_infos,
                )
            ]

        tools = self._client_manager.get_all_tools()

        # Filter by policy
        tools = [
            t
            for t in tools
            if self._policy_manager.is_server_allowed(t.server_name)
            and self._policy_manager.is_tool_allowed(t.tool_id)
        ]

        # Filter by server online status OR merge cached tools
        if parsed.include_offline:
            # Merge cached tools for offline servers
            cached_tools = self._get_cached_tools_for_offline_servers()
            # Build set of existing tool_ids to avoid duplicates
            existing_ids = {t.tool_id for t in tools}
            # Add cached tools that aren't already present
            for ct in cached_tools:
                if ct.tool_id not in existing_ids:
                    tools.append(ct)
        else:
            # Default: only online servers
            tools = [
                t for t in tools if self._client_manager.is_server_online(t.server_name)
            ]

        total_available = len(tools)

        # Capture servers already represented by online/cached tools BEFORE any
        # narrowing filters, so manifest candidates only cover "never started"
        # servers (no cached tools at all) rather than ones merely filtered out.
        represented_servers = {t.server_name for t in tools}

        # Filter by server name
        if parsed.filters and parsed.filters.server:
            tools = [t for t in tools if t.server_name == parsed.filters.server]

        # Filter by tags
        if parsed.filters and parsed.filters.tags:
            filter_tags = [tag.lower() for tag in parsed.filters.tags]
            tools = [t for t in tools if any(tag in t.tags for tag in filter_tags)]

        # Filter by max risk level
        if parsed.filters and parsed.filters.risk_max:
            max_risk = RISK_ORDER.get(parsed.filters.risk_max, 4)
            tools = [
                t for t in tools if RISK_ORDER.get(t.risk_hint.value, 4) <= max_risk
            ]

        # Text search (if query provided) - word-based matching
        if parsed.query and not scoped_advisor:
            query_words = parsed.query.lower().split()
            tools = [
                t
                for t in tools
                if any(
                    word in t.tool_name.lower()
                    or word in t.short_description.lower()
                    or any(word in tag for tag in t.tags)
                    for word in query_words
                )
            ]

        # Sort by relevance (if query) or alphabetically
        if parsed.query:
            query_lower = parsed.query.lower()

            def sort_key(t: Any) -> tuple[int, int, str, str]:
                exact = t.tool_name.lower() == query_lower
                starts = t.tool_name.lower().startswith(query_lower)
                return (
                    0 if exact else 1,
                    0 if starts else 1,
                    t.server_name,
                    t.tool_id,
                )

            tools.sort(key=sort_key)
        else:
            tools.sort(key=lambda t: (t.server_name, t.tool_id))

        # Apply limit
        registry_candidates: list[CapabilityCandidate] = []
        if parsed.query and not scoped_advisor:
            registry_candidates = await self._registry_candidates_for_query(
                parsed.query, limit=min(5, parsed.limit)
            )

        # Surface manifest-provisionable servers that have never been started
        # (no cached tools) so agents can provision them directly (#78).
        manifest_candidates: list[CapabilityCandidate] = []
        if parsed.include_offline and parsed.query and not scoped_advisor:
            manifest_candidates = self._manifest_candidates_for_query(
                parsed.query,
                manifest=load_manifest(),
                configured_servers=self._load_configured_servers(),
                exclude_servers=represented_servers,
                limit=min(5, parsed.limit),
            )

        truncated = len(tools) > parsed.limit
        tools = tools[: parsed.limit]

        # Convert to capability cards
        results = []
        for t in tools:
            # Get code hint if guidance enabled
            code_hint = None
            if self._guidance_config and self._guidance_config.include_code_hints:
                code_hint = get_code_hint(t.tool_id, t.tool_name, t.short_description)
                # Trim to max length if configured
                if code_hint and len(code_hint) > self._guidance_config.max_hint_length:
                    code_hint = code_hint[: self._guidance_config.max_hint_length]

            results.append(
                CapabilityCard(
                    tool_id=t.tool_id,
                    server=t.server_name,
                    tool_name=t.tool_name,
                    title=t.title,
                    short_description=t.short_description,
                    tags=t.tags,
                    availability="online"
                    if self._client_manager.is_server_online(t.server_name)
                    else "offline",
                    risk_hint=t.risk_hint.value,
                    icons=t.icons,
                    execution=t.execution,
                    schema_dialect=t.schema_dialect,
                    code_hint=code_hint,
                )
            )

        return CatalogSearchOutput(
            results=results,
            total_available=total_available,
            truncated=truncated,
            cli_hints=cli_hints,
            registry_candidates=registry_candidates,
            manifest_candidates=manifest_candidates,
        )

    async def describe(self, input_data: dict[str, Any]) -> SchemaCard:
        """gateway.describe - Get detailed info about a tool."""
        parsed = DescribeInput.model_validate(input_data)

        # Match invoke: deny before registry lookup or lazy-start side effects.
        if not self._policy_manager.is_tool_allowed(parsed.tool_id):
            raise GatewayException(
                ErrorCode.E402_TOOL_DENIED,
                details={"tool_id": parsed.tool_id},
            )

        tool_info = self._client_manager.get_tool(parsed.tool_id)

        if not tool_info:
            # Tool not in registry - check if from lazy server
            if "::" in parsed.tool_id:
                server_name = parsed.tool_id.split("::")[0]
                if self._client_manager.is_lazy_server(server_name):
                    # Connect server to get tool info
                    connected = await self._ensure_server_for_tool(parsed.tool_id)
                    if connected:
                        tool_info = self._client_manager.get_tool(parsed.tool_id)

            if not tool_info:
                raise GatewayException(
                    ErrorCode.E301_TOOL_NOT_FOUND,
                    details={"tool_id": parsed.tool_id},
                )

        self._record_feedback_event(
            "invoke_attempt",
            {
                "tool_id": parsed.tool_id,
                "server": tool_info.server_name,
            },
        )

        # Extract args from schema
        args: list[ArgInfo] = []
        schema = tool_info.input_schema
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        nested_placeholders: dict[str, str] = {}
        for name, prop in properties.items():
            description = prop.get("description", "")
            type_str, item_schema, placeholder = _summarize_arg_schema(prop)
            if item_schema is not None:
                nested_placeholders[name] = placeholder

            args.append(
                ArgInfo(
                    name=name,
                    type=type_str,
                    required=name in required,
                    short_description=description[:200] if description else "",
                    examples=prop.get("examples"),
                    item_schema=item_schema,
                )
            )

        # Generate safety notes based on risk
        safety_notes: list[str] = []
        if tool_info.risk_hint.value == "high":
            safety_notes.append("This tool may modify data or have side effects.")

        # Build invoke template for direct invocation
        arg_placeholders: dict[str, str] = {}
        for arg in args:
            if arg.item_schema is not None:
                arg_placeholders[arg.name] = nested_placeholders[arg.name]
            elif arg.required:
                arg_placeholders[arg.name] = f"<required: {arg.type}>"
            else:
                arg_placeholders[arg.name] = f"<optional: {arg.type}>"

        invoke_template = InvokeTemplate(
            tool_id=tool_info.tool_id,
            arguments=arg_placeholders,
        )

        # Get code snippet if guidance enabled
        code_snippet = None
        if self._guidance_config and self._guidance_config.include_code_snippets:
            # Static code-snippet template lookup (None when no template exists)
            code_snippet = get_code_snippet(
                tool_info.tool_id,
                max_lines=self._guidance_config.max_snippet_lines,
                tool_info=tool_info,
            )

        self._record_feedback_event(
            "describe",
            {"tool_id": parsed.tool_id, "server": tool_info.server_name},
        )

        return SchemaCard(
            server=tool_info.server_name,
            tool_name=tool_info.tool_name,
            title=tool_info.title,
            description=tool_info.description,
            icons=tool_info.icons,
            args=args,
            output_schema=tool_info.output_schema,
            annotations=tool_info.annotations,
            execution=tool_info.execution,
            schema_dialect=tool_info.schema_dialect,
            safety_notes=safety_notes if safety_notes else None,
            invoke_template=invoke_template,
            code_snippet=code_snippet,
            # No feedback hint on a SUCCESSFUL describe. `_feedback_hint()`
            # opens with "Technical failure detected", which is false here and
            # was reaching every client on every successful describe. The
            # convention is already correct on invoke's success path (it passes
            # None); this site just did not follow it. Describe's own error
            # paths below still emit it.
            feedback_hint=None,
        )

    async def invoke(self, input_data: dict[str, Any]) -> InvokeOutput:
        """gateway.invoke - Call a downstream tool."""
        audit_started_at = time.monotonic()
        trace_context = self._extract_trace_context(input_data)
        parsed = InvokeInput.model_validate(input_data)

        # Policy denial must happen before registry lookup or lazy-start so an
        # out-of-scope server cannot be started as a side effect of inspection.
        if not self._policy_manager.is_tool_allowed(parsed.tool_id):
            server_name = parsed.tool_id.split("::", 1)[0]
            error = make_error(
                ErrorCode.E402_TOOL_DENIED,
                tool_id=parsed.tool_id,
            )
            self._audit(
                method="gateway.invoke",
                action="invoke",
                outcome="refused",
                started_at=audit_started_at,
                server_name=server_name,
                tool_id=parsed.tool_id,
                auth_state="policy_denied",
                auth_event="policy_denied",
                error=error.message,
                trace_context=trace_context,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
                auth_state="policy_denied",
                feedback_hint=self._feedback_hint(),
            )

        # Check if tool exists in registry
        tool_info = self._client_manager.get_tool(parsed.tool_id)

        if not tool_info:
            # Tool not in registry - check if it might be from a lazy server
            # that hasn't connected yet (no tools indexed)
            if "::" in parsed.tool_id:
                server_name = parsed.tool_id.split("::")[0]
                if self._client_manager.is_lazy_server(server_name):
                    # Try to connect the lazy server first
                    try:
                        connected = await self._ensure_server_for_tool(parsed.tool_id)
                    except MissingRemoteHeaderAuthError as e:
                        env_names = ", ".join(e.missing_env_vars)
                        error = make_error(
                            ErrorCode.E201_SERVER_OFFLINE,
                            message=(
                                "Missing remote header environment variable(s) "
                                f"for '{server_name}': {env_names}"
                            ),
                            tool_id=parsed.tool_id,
                        )
                        self._audit(
                            method="gateway.invoke",
                            action="invoke",
                            outcome="failure",
                            started_at=audit_started_at,
                            server_name=server_name,
                            tool_id=parsed.tool_id,
                            auth_state="missing_auth",
                            auth_event="missing_credential",
                            error=error.message,
                            trace_context=trace_context,
                        )
                        return InvokeOutput(
                            tool_id=parsed.tool_id,
                            ok=False,
                            truncated=False,
                            raw_size_estimate=0,
                            errors=[error.model_dump_json()],
                            missing_env_vars=e.missing_env_vars,
                            auth_state="missing_auth",
                            auth_methods=self._auth_methods_for_server(server_name),
                            next_step=(
                                f"gateway.auth_connect(server_name='{server_name}')"
                            ),
                            feedback_hint=self._feedback_hint(),
                        )
                    if connected:
                        # Re-check for tool after connection
                        tool_info = self._client_manager.get_tool(parsed.tool_id)

            if not tool_info:
                error = make_error(
                    ErrorCode.E301_TOOL_NOT_FOUND,
                    tool_id=parsed.tool_id,
                )
                self._audit(
                    method="gateway.invoke",
                    action="invoke",
                    outcome="failure",
                    started_at=audit_started_at,
                    tool_id=parsed.tool_id,
                    error=error.message,
                    trace_context=trace_context,
                )
                return InvokeOutput(
                    tool_id=parsed.tool_id,
                    ok=False,
                    truncated=False,
                    raw_size_estimate=0,
                    errors=[error.model_dump_json()],
                    feedback_hint=self._feedback_hint(),
                )

        # Pre-dispatch: validate required arguments
        required = (tool_info.input_schema or {}).get("required", [])
        missing = [f for f in required if f not in (parsed.arguments or {})]
        if missing:
            error = make_error(
                ErrorCode.E304_INVALID_ARGUMENTS,
                message=f"Missing required arguments: {', '.join(missing)}",
                tool_id=parsed.tool_id,
            )
            self._audit(
                method="gateway.invoke",
                action="invoke",
                outcome="failure",
                started_at=audit_started_at,
                server_name=tool_info.server_name,
                tool_id=parsed.tool_id,
                protocol_version=self._protocol_version(tool_info.server_name),
                error=error.message,
                trace_context=trace_context,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
                feedback_hint=self._feedback_hint(),
            )

        # Call the tool
        _call_start = time.monotonic()
        timeout_ms = 30000
        if parsed.options:
            timeout_ms = parsed.options.timeout_ms
        try:
            if parsed.task is None:
                if trace_context is None:
                    result = await self._client_manager.call_tool(
                        parsed.tool_id, parsed.arguments, timeout_ms
                    )
                else:
                    result = await self._client_manager.call_tool(
                        parsed.tool_id,
                        parsed.arguments,
                        timeout_ms,
                        trace_context=trace_context,
                    )
            else:
                if trace_context is None:
                    result = await self._client_manager.call_tool(
                        parsed.tool_id, parsed.arguments, timeout_ms, task=parsed.task
                    )
                else:
                    result = await self._client_manager.call_tool(
                        parsed.tool_id,
                        parsed.arguments,
                        timeout_ms,
                        task=parsed.task,
                        trace_context=trace_context,
                    )

            task_info = None
            if isinstance(result, dict):
                task_payload = result.get("task")
                if isinstance(task_payload, dict):
                    task_id = task_payload.get("taskId") or task_payload.get("task_id")
                    if isinstance(task_id, str):
                        task_info = self._client_manager.get_task_record(
                            tool_info.server_name, task_id
                        )

            # Process output (truncate, redact)
            max_bytes = None
            if parsed.options and parsed.options.max_output_chars:
                max_bytes = parsed.options.max_output_chars * 4  # Rough bytes estimate

            redact = (
                parsed.options.redact_secrets
                if parsed.options
                else task_info is not None
            )

            processed = self._policy_manager.process_output(
                result, redact=redact, max_bytes=max_bytes
            )
            public_task = (
                self._sanitize_task_for_output(task_info)
                if task_info is not None
                else None
            )

            elapsed_ms = round((time.monotonic() - _call_start) * 1000)
            logger.info(
                f"tool_call tool={parsed.tool_id} server={tool_info.server_name} ok=True elapsed_ms={elapsed_ms}"
            )
            self._audit(
                method="gateway.invoke",
                action="invoke",
                outcome="success",
                started_at=audit_started_at,
                server_name=tool_info.server_name,
                tool_id=parsed.tool_id,
                task_id=task_info.task_id if task_info else None,
                protocol_version=self._protocol_version(tool_info.server_name),
                trace_context=trace_context,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=True,
                result=None if task_info is not None else processed["result"],
                task=public_task,
                truncated=processed["truncated"],
                summary=processed["summary"],
                raw_size_estimate=processed["raw_size"],
                feedback_hint=None,
            )

        except TimeoutError:
            self._record_feedback_event(
                "invoke_failure",
                {"tool_id": parsed.tool_id, "reason": "timeout"},
            )
            elapsed_ms = round((time.monotonic() - _call_start) * 1000)
            logger.info(
                f"tool_call tool={parsed.tool_id} server={tool_info.server_name} ok=False elapsed_ms={elapsed_ms} reason=timeout"
            )
            error = make_error(
                ErrorCode.E303_TOOL_TIMEOUT,
                tool_id=parsed.tool_id,
                timeout_ms=timeout_ms,
            )
            self._audit(
                method="gateway.invoke",
                action="invoke",
                outcome="failure",
                started_at=audit_started_at,
                server_name=tool_info.server_name,
                tool_id=parsed.tool_id,
                protocol_version=self._protocol_version(tool_info.server_name),
                error=error.message,
                trace_context=trace_context,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
                feedback_hint=self._feedback_hint(),
            )

        except ConnectionError as e:
            auth_challenge = self._auth_challenge_from_message(str(e))
            auth_state = "none"
            if auth_challenge:
                auth_state = (
                    "insufficient_scope"
                    if auth_challenge.missing_scopes
                    else "missing_auth"
                )
            self._record_feedback_event(
                "invoke_failure",
                {
                    "tool_id": parsed.tool_id,
                    "reason": "connection_error",
                    "error": self._sanitize_error(e),
                },
            )
            elapsed_ms = round((time.monotonic() - _call_start) * 1000)
            logger.info(
                f"tool_call tool={parsed.tool_id} server={tool_info.server_name} ok=False elapsed_ms={elapsed_ms} reason=connection_error"
            )
            error = make_error(
                ErrorCode.E201_SERVER_OFFLINE,
                message=self._sanitize_error(e),
                tool_id=parsed.tool_id,
            )
            self._audit(
                method="gateway.invoke",
                action="invoke",
                outcome="failure",
                started_at=audit_started_at,
                server_name=tool_info.server_name,
                tool_id=parsed.tool_id,
                protocol_version=self._protocol_version(tool_info.server_name),
                auth_state=auth_state,
                auth_event=self._auth_event_for_challenge(auth_challenge),
                error=error.message,
                trace_context=trace_context,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
                auth_state=cast(Any, auth_state),
                auth_challenge=auth_challenge,
                next_step="Resolve remote authorization out of band, then retry gateway.invoke."
                if auth_challenge
                else None,
                feedback_hint=self._feedback_hint(),
            )

        except Exception as e:
            url_elicitations = parse_url_elicitation_error(e)
            if url_elicitations:
                self._record_feedback_event(
                    "invoke_failure",
                    {"tool_id": parsed.tool_id, "reason": "url_elicitation_required"},
                )
                self._audit(
                    method="gateway.invoke",
                    action="invoke",
                    outcome="failure",
                    started_at=audit_started_at,
                    server_name=tool_info.server_name,
                    tool_id=parsed.tool_id,
                    protocol_version=self._protocol_version(tool_info.server_name),
                    auth_state="elicitation_required",
                    auth_event="url_elicitation_required",
                    error="URL-mode elicitation required.",
                    trace_context=trace_context,
                )
                return InvokeOutput(
                    tool_id=parsed.tool_id,
                    ok=False,
                    truncated=False,
                    raw_size_estimate=0,
                    errors=["URL-mode elicitation required."],
                    auth_state="elicitation_required",
                    url_elicitations=url_elicitations,
                    next_step=url_elicitations[0].next_step,
                    feedback_hint=self._feedback_hint(),
                )
            auth_challenge = self._auth_challenge_from_message(str(e))
            auth_state = "none"
            if auth_challenge:
                auth_state = (
                    "insufficient_scope"
                    if auth_challenge.missing_scopes
                    else "missing_auth"
                )
            self._record_feedback_event(
                "invoke_failure",
                {
                    "tool_id": parsed.tool_id,
                    "reason": "exception",
                    "error": self._sanitize_error(e),
                },
            )
            elapsed_ms = round((time.monotonic() - _call_start) * 1000)
            logger.info(
                f"tool_call tool={parsed.tool_id} server={tool_info.server_name} ok=False elapsed_ms={elapsed_ms} reason=exception"
            )
            error = make_error(
                ErrorCode.E302_TOOL_EXECUTION_FAILED,
                message=self._sanitize_error(e),
                tool_id=parsed.tool_id,
            )
            self._audit(
                method="gateway.invoke",
                action="invoke",
                outcome="failure",
                started_at=audit_started_at,
                server_name=tool_info.server_name,
                tool_id=parsed.tool_id,
                protocol_version=self._protocol_version(tool_info.server_name),
                auth_state=auth_state,
                auth_event=self._auth_event_for_challenge(auth_challenge),
                error=error.message,
                trace_context=trace_context,
            )
            return InvokeOutput(
                tool_id=parsed.tool_id,
                ok=False,
                truncated=False,
                raw_size_estimate=0,
                errors=[error.model_dump_json()],
                auth_state=cast(Any, auth_state),
                auth_challenge=auth_challenge,
                next_step="Resolve remote authorization out of band, then retry gateway.invoke."
                if auth_challenge
                else None,
                feedback_hint=self._feedback_hint(),
            )

    async def refresh(self, input_data: dict[str, Any]) -> RefreshOutput:
        """gateway.refresh - Reload backend configs and reconnect."""
        audit_started_at = time.monotonic()
        parsed = RefreshInput.model_validate(input_data)

        logger.info(f"Refresh requested: {parsed.reason or 'manual refresh'}")
        pending_seen = 0
        pending_cancelled = 0
        active_tasks_seen = 0
        active_tasks_cancelled = 0

        try:
            configs = load_configs(
                project_root=self._project_root,
                custom_config_path=self._custom_config_path,
            )

            # Filter out the gateway itself to prevent recursive connection
            # Uses command-based detection, not just name matching
            configs = filter_self_references(configs)

            manifest_servers = {}
            try:
                manifest = load_manifest()
                manifest_servers = manifest.servers
            except Exception as e:
                logger.warning(f"Failed to load manifest startup configs: {e}")

            provisioned: dict[str, str | None] = {}
            try:
                provisioned = self._load_provisioned_registry()
            except Exception as e:
                logger.warning(f"Failed to restore provisioned servers: {e}")

            enabled_auto_start = load_enabled_auto_start(
                project_root=self._project_root,
                custom_config_path=self._custom_config_path,
            )
            disabled_auto_start = load_disabled_auto_start(
                project_root=self._project_root,
                custom_config_path=self._custom_config_path,
            )
            resolution = resolve_startup_configs(
                configs,
                manifest_servers=manifest_servers,
                enabled_auto_start=enabled_auto_start,
                disabled_auto_start=disabled_auto_start,
                provisioned_server_names=set(provisioned),
                is_server_allowed=self._policy_manager.is_server_allowed,
                is_auth_available=self._check_api_key_available,
                legacy_manifest_auto_start=is_legacy_manifest_auto_start_enabled(),
                project_root=self._project_root,
            )

            counts = summarize_startup_resolution(resolution)
            logger.info(
                "Refresh policy summary: "
                f"eager={counts['eager']}, lazy={counts['lazy']}, "
                f"skipped={counts['skipped']}, "
                f"policy_denied={counts['policy_denied']}, "
                f"missing_auth={counts['missing_auth']}, "
                f"unknown_auto_start={counts['unknown_auto_start']}"
            )
            for skipped in resolution.skipped:
                if skipped.reason == StartupSkipReason.MISSING_AUTH:
                    logger.info(
                        f"Skipping refresh entry '{skipped.name}' from {skipped.source}: "
                        f"missing_auth; set {skipped.env_var} to enable eager startup"
                    )
                elif skipped.reason == StartupSkipReason.UNKNOWN_AUTO_START:
                    logger.info(
                        f"Skipping refresh entry '{skipped.name}' from {skipped.source}: "
                        "unknown_auto_start; add a matching mcpServers entry or remove it from autoStart"
                    )
                else:
                    logger.info(
                        f"Skipping refresh entry '{skipped.name}' from {skipped.source}: "
                        f"{skipped.reason.value}"
                    )

            pending_requests = self._client_manager.get_pending_requests()
            pending_seen = len(pending_requests)
            active_tasks = self._client_manager.get_active_tasks()
            active_tasks_seen = len(active_tasks)
            if (pending_seen or active_tasks_seen) and not parsed.force:
                revision_id, _ = self._client_manager.get_registry_meta()
                statuses = self._client_manager.get_all_server_statuses()
                logger.warning(
                    "Refresh refused with "
                    f"{pending_seen} pending downstream requests and "
                    f"{active_tasks_seen} active MCP tasks"
                )
                self._audit(
                    method="gateway.refresh",
                    action="refresh",
                    outcome="refused",
                    started_at=audit_started_at,
                    error="pending requests or active MCP tasks",
                )
                return RefreshOutput(
                    ok=False,
                    servers_seen=len(statuses),
                    servers_online=sum(
                        1 for s in statuses if s.status.value == "online"
                    ),
                    tools_indexed=len(self._client_manager.get_all_tools()),
                    revision_id=revision_id,
                    errors=[
                        "Refresh refused because downstream requests or active MCP tasks are pending. "
                        "Use gateway.list_pending to inspect them or retry with force=true "
                        "to cancel them before refreshing."
                    ],
                    pending_requests_seen=pending_seen,
                    pending_requests_refused=pending_seen,
                    pending_requests_remaining=pending_seen,
                    mcp_tasks_seen=active_tasks_seen,
                    mcp_tasks_refused=active_tasks_seen,
                    mcp_tasks_remaining=active_tasks_seen,
                )

            if pending_seen:
                pending_cancelled = self._client_manager.cancel_all_pending_requests()
                logger.warning(
                    f"Forced refresh cancelled {pending_cancelled} pending downstream requests"
                )
            if active_tasks_seen:
                (
                    active_tasks_cancelled,
                    task_errors,
                ) = await self._client_manager.cancel_active_tasks()
                if task_errors:
                    revision_id, _ = self._client_manager.get_registry_meta()
                    statuses = self._client_manager.get_all_server_statuses()
                    self._audit(
                        method="gateway.refresh",
                        action="refresh",
                        outcome="failure",
                        started_at=audit_started_at,
                        error="; ".join(task_errors),
                    )
                    return RefreshOutput(
                        ok=False,
                        servers_seen=len(statuses),
                        servers_online=sum(
                            1 for s in statuses if s.status.value == "online"
                        ),
                        tools_indexed=len(self._client_manager.get_all_tools()),
                        revision_id=revision_id,
                        errors=task_errors,
                        pending_requests_seen=pending_seen,
                        pending_requests_cancelled=pending_cancelled,
                        mcp_tasks_seen=active_tasks_seen,
                        mcp_tasks_cancelled=active_tasks_cancelled,
                        mcp_tasks_remaining=len(
                            self._client_manager.get_active_tasks()
                        ),
                    )

            self.set_startup_observations(
                build_startup_observation_snapshot(resolution)
            )

            # Diff-based refresh: leave servers whose resolved config is
            # unchanged connected and running. Only disconnect servers that were
            # removed (or are no longer in the resolved set at all, e.g. now
            # policy-denied/missing-auth) or whose config changed, and only
            # eagerly connect newly-added/changed eager servers. This avoids
            # dropping previously-running lazy/provisioned servers to offline
            # and avoids needlessly respawning unchanged processes (e.g. a live
            # browser) on every refresh.
            #
            # Equality uses _refresh_config_unchanged, which compares the fields
            # that affect the spawned process (command/args/cwd for local,
            # url/headers/transport for remote). The keep-set is the union of
            # eager AND lazy: a lazily-started server (e.g. a provisioned browser
            # put into _clients by ensure_connected) resolves as lazy here, and
            # must NOT be torn down.
            current_configs = self._client_manager.get_connected_configs()
            header_env_lookup = build_remote_header_env_lookup(self._project_root)
            eager_by_name = {config.name: config for config in resolution.eager_configs}
            keep_by_name = {
                config.name: config
                for config in resolution.eager_configs + resolution.lazy_configs
            }

            def _live_keep(name: str) -> bool:
                # Keep a server connected only if it is actually ONLINE, still in
                # the resolved keep-set, and its (resolved) config is unchanged.
                # A present-but-not-ONLINE entry (e.g. a crashed eager server left
                # in _clients with status ERROR after its reconnect attempts
                # exhausted) is NOT a live keep — it is torn down here and, if
                # eager, reconnected below, so `gateway.refresh` heals it again.
                return (
                    name in keep_by_name
                    and self._client_manager.is_server_online(name)
                    and _refresh_config_unchanged(
                        current_configs[name],
                        keep_by_name[name],
                        header_env_lookup=header_env_lookup,
                        old_resolved_headers=(
                            self._client_manager.get_connected_resolved_headers(name)
                        ),
                    )
                )

            to_disconnect = [name for name in current_configs if not _live_keep(name)]
            to_connect = [
                config
                for name, config in eager_by_name.items()
                if not (
                    name in current_configs
                    and self._client_manager.is_server_online(name)
                    and _refresh_config_unchanged(
                        current_configs[name],
                        config,
                        header_env_lookup=header_env_lookup,
                        old_resolved_headers=(
                            self._client_manager.get_connected_resolved_headers(name)
                        ),
                    )
                )
            ]

            for name in to_disconnect:
                # Pending/active work was already gated (and force-cancelled)
                # above, so force the per-server disconnect here.
                await self._client_manager.disconnect_server(name, force=True)

            self._client_manager.register_lazy_configs(resolution.lazy_configs)
            # Reconcile the lazy set to the resolved keep-set: drop on-demand
            # entries for servers that are now removed / policy-denied /
            # missing-auth (and undo disconnect_server's re-registration of the
            # ones we just tore down) so they can no longer be lazily started via
            # ensure_connected().
            self._client_manager.prune_lazy_configs(set(keep_by_name))

            errors: list[str] = []
            if to_connect:
                errors = await self._client_manager.connect_all(to_connect)
            pending_remaining = len(self._client_manager.get_pending_requests())

            revision_id, _ = self._client_manager.get_registry_meta()
            statuses = self._client_manager.get_all_server_statuses()
            resolved_names = {
                config.name
                for config in resolution.lazy_configs + resolution.eager_configs
            }
            resolved_names.update(skip.name for skip in resolution.skipped)

            output = RefreshOutput(
                ok=len(errors) == 0,
                servers_seen=len(resolved_names),
                servers_online=sum(1 for s in statuses if s.status.value == "online"),
                tools_indexed=len(self._client_manager.get_all_tools()),
                revision_id=revision_id,
                errors=errors if errors else None,
                pending_requests_seen=pending_seen,
                pending_requests_cancelled=pending_cancelled,
                pending_requests_remaining=pending_remaining,
                mcp_tasks_seen=active_tasks_seen,
                mcp_tasks_cancelled=active_tasks_cancelled,
                mcp_tasks_remaining=len(self._client_manager.get_active_tasks()),
            )
            self._audit(
                method="gateway.refresh",
                action="refresh",
                outcome="success" if output.ok else "failure",
                started_at=audit_started_at,
                error="; ".join(errors) if errors else None,
            )
            return output

        except Exception as e:
            self._audit(
                method="gateway.refresh",
                action="refresh",
                outcome="failure",
                started_at=audit_started_at,
                error=str(e),
            )
            return RefreshOutput(
                ok=False,
                servers_seen=0,
                servers_online=0,
                tools_indexed=0,
                revision_id="error",
                errors=[str(e)],
                pending_requests_seen=pending_seen,
                pending_requests_cancelled=pending_cancelled,
                mcp_tasks_seen=active_tasks_seen,
                mcp_tasks_cancelled=active_tasks_cancelled,
            )

    async def health(self) -> HealthOutput:
        """gateway.health - Get gateway health status."""
        revision_id, last_refresh_ts = self._client_manager.get_registry_meta()
        statuses = self._client_manager.get_all_server_statuses()
        servers: list[ServerHealthInfo] = []

        for status in statuses:
            info = ServerHealthInfo(
                name=status.name,
                status=status.status.value,
                tool_count=status.tool_count,
                protocol_version=status.protocol_version,
                server_capabilities=status.server_capabilities,
                error=status.last_error
                if status.status.value == "error" and status.last_error
                else None,
            )
            observation = self._startup_observations.get(status.name)
            if observation:
                info.startup_policy = observation.startup_policy
                info.startup_source = observation.startup_source
                info.startup_skip_reason = observation.startup_skip_reason
                info.startup_env_var = observation.startup_env_var
                info.missing_env_vars = observation.missing_env_vars or []
                if observation.startup_skip_reason in {
                    StartupSkipReason.MISSING_AUTH.value,
                    StartupSkipReason.POLICY_DENIED.value,
                }:
                    info.auth_state = cast(Any, observation.startup_skip_reason)
                    if (
                        observation.startup_skip_reason
                        == StartupSkipReason.MISSING_AUTH.value
                    ):
                        info.auth_methods = self._auth_methods_for_server(status.name)
                        info.next_step = (
                            f"gateway.auth_connect(server_name='{status.name}')"
                        )
            if status.last_error:
                auth_challenge = self._auth_challenge_from_message(status.last_error)
                if auth_challenge:
                    info.auth_challenge = auth_challenge
                    info.auth_state = (
                        "insufficient_scope"
                        if auth_challenge.missing_scopes
                        else "missing_auth"
                    )
                    info.next_step = "Resolve remote authorization out of band, then retry connection."
            servers.append(info)

        known_names = {s.name for s in servers}

        # Include provisioned servers that are not currently tracked (e.g. after restart)
        provisioned = self._load_provisioned_registry()
        for prov_name in provisioned:
            if prov_name in known_names:
                continue
            info = ServerHealthInfo(name=prov_name, status="offline", tool_count=0)
            observation = self._startup_observations.get(prov_name)
            if observation:
                info.startup_policy = observation.startup_policy
                info.startup_source = observation.startup_source
                info.startup_skip_reason = observation.startup_skip_reason
                info.startup_env_var = observation.startup_env_var
                info.missing_env_vars = observation.missing_env_vars or []
                if observation.startup_skip_reason in {
                    StartupSkipReason.MISSING_AUTH.value,
                    StartupSkipReason.POLICY_DENIED.value,
                }:
                    info.auth_state = cast(Any, observation.startup_skip_reason)
                    if (
                        observation.startup_skip_reason
                        == StartupSkipReason.MISSING_AUTH.value
                    ):
                        info.auth_methods = self._auth_methods_for_server(prov_name)
                        info.next_step = (
                            f"gateway.auth_connect(server_name='{prov_name}')"
                        )
            servers.append(info)
            known_names.add(prov_name)

        for name, observation in self._startup_observations.items():
            if name in known_names or observation.startup_policy != "skipped":
                continue
            servers.append(
                ServerHealthInfo(
                    name=name,
                    status="offline",
                    tool_count=0,
                    startup_policy=observation.startup_policy,
                    startup_source=observation.startup_source,
                    startup_skip_reason=observation.startup_skip_reason,
                    startup_env_var=observation.startup_env_var,
                    missing_env_vars=observation.missing_env_vars or [],
                    auth_state=cast(
                        Any,
                        observation.startup_skip_reason
                        if observation.startup_skip_reason
                        in {
                            StartupSkipReason.MISSING_AUTH.value,
                            StartupSkipReason.POLICY_DENIED.value,
                        }
                        else "none",
                    ),
                    next_step=(
                        f"gateway.auth_connect(server_name='{name}')"
                        if observation.startup_skip_reason
                        == StartupSkipReason.MISSING_AUTH.value
                        else None
                    ),
                )
            )

        diagnostics = self._transport_diagnostics.model_copy()
        diagnostics.audit_buffer_size = self._audit_events.maxlen or len(
            self._audit_events
        )
        diagnostics.auth_metadata_present = any(
            server.auth_metadata is not None or server.auth_challenge is not None
            for server in servers
        )
        diagnostics.protocol_version_visible = any(
            server.protocol_version for server in servers
        )
        diagnostics.npm_identity = get_resolver().status_summary()

        servers.sort(key=lambda server: server.name)
        return HealthOutput(
            revision_id=revision_id,
            servers=servers,
            last_refresh_ts=last_refresh_ts,
            gateway_diagnostics=diagnostics,
            audit_events=list(self._audit_events) or None,
        )

    def _config_source_paths_by_server(self) -> dict[str, tuple[str, str]]:
        paths: dict[str, tuple[str, str]] = {}
        for source in load_config_sources(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        ):
            if not source.config:
                continue
            for name in source.config.mcpServers:
                paths.setdefault(name, (source.source, str(source.path)))
        return paths

    async def config_status(self) -> ConfigStatusOutput:
        """gateway.config_status - Read-only effective config administration."""
        configured = load_configs(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )
        manifest = load_manifest().servers
        provisioned = self._load_provisioned_registry()
        discovered = self._discovered_server_configs
        enabled = load_enabled_auto_start(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )
        disabled = load_disabled_auto_start(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )
        resolution = resolve_startup_configs(
            configured,
            manifest_servers={**manifest, **discovered},
            enabled_auto_start=enabled,
            disabled_auto_start=disabled,
            provisioned_server_names=provisioned,
            is_server_allowed=self._policy_manager.is_server_allowed,
            is_auth_available=lambda env_var: bool(os.environ.get(env_var)),
            legacy_manifest_auto_start=is_legacy_manifest_auto_start_enabled(),
            project_root=self._project_root,
        )
        observations = build_startup_observation_snapshot(resolution)
        source_paths = self._config_source_paths_by_server()
        health_by_name = {
            server.name: server for server in (await self.health()).servers
        }
        config_sources = load_config_sources(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )
        known_names = (
            {config.name for config in configured}
            | set(manifest)
            | set(provisioned)
            | set(discovered)
            | set(observations)
        )
        entries: list[EffectiveConfigEntry] = []
        for name in sorted(known_names):
            observation = observations.get(name)
            health = health_by_name.get(name)
            source_name, source_path = source_paths.get(name, (None, None))
            diagnostics: list[str] = []
            auth_state = health.auth_state if health else "none"
            status = health.status if health else "offline"
            startup_policy = observation.startup_policy if observation else "unknown"
            startup_source = observation.startup_source if observation else None
            skip_reason = observation.startup_skip_reason if observation else None
            env_var = observation.startup_env_var if observation else None
            missing_env_vars = (
                observation.missing_env_vars if observation else []
            ) or []
            if skip_reason:
                diagnostics.append(skip_reason)
            if name in enabled and name in disabled:
                diagnostics.append("auto_start_disabled_conflict")
            entries.append(
                EffectiveConfigEntry(
                    name=name,
                    status=status,
                    startup_policy=startup_policy,
                    startup_source=startup_source,
                    source=cast(Any, source_name or startup_source),
                    source_path=source_path,
                    startup_skip_reason=skip_reason,
                    startup_env_var=env_var,
                    missing_env_vars=missing_env_vars,
                    auth_state=auth_state,
                    configured=any(config.name == name for config in configured),
                    manifest=name in manifest,
                    provisioned=name in provisioned,
                    discovered=name in discovered,
                    diagnostics=diagnostics,
                )
            )
        policy = get_startup_policy(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
            known_server_names=known_names,
        )
        return ConfigStatusOutput(
            entries=entries,
            sources=[source.info() for source in config_sources],
            diagnostics=[diagnostic.message for diagnostic in policy.diagnostics],
        )

    async def get_startup_policy(self) -> StartupPolicyOutput:
        """gateway.get_startup_policy - Read persisted startup policy."""
        configured = load_configs(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )
        manifest = load_manifest().servers
        known_names = {config.name for config in configured} | set(manifest)
        return get_startup_policy(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
            known_server_names=known_names,
        )

    async def set_startup_policy(
        self, input_data: dict[str, Any]
    ) -> StartupPolicyPreview:
        """gateway.set_startup_policy - Preview/apply an autoStart mutation."""
        operation = StartupPolicyOperation.model_validate(input_data)
        return set_startup_policy(
            operation,
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )

    async def connect_server(self, input_data: dict[str, Any]) -> LifecycleServerOutput:
        """gateway.connect_server - Connect one known server."""
        parsed = ConnectServerInput.model_validate(input_data)
        server_name = parsed.server_name
        prior_status = self._status_value(server_name)

        config, failure = self._resolve_lifecycle_config(
            server_name, action="connect", prior_status=prior_status
        )
        if failure is not None:
            return failure
        if config is None:
            return self._lifecycle_output(
                ok=False,
                server=server_name,
                action="connect",
                prior_status=prior_status,
                message=f"Server '{server_name}' could not be resolved.",
                errors=[f"Unable to resolve server: {server_name}"],
            )

        if self._client_manager.is_server_online(server_name):
            return self._lifecycle_output(
                ok=True,
                server=server_name,
                action="connect",
                prior_status=prior_status,
                message=f"Server '{server_name}' is already online.",
            )

        errors = await self._client_manager.connect_server(config)
        url_elicitations = parse_url_elicitation_error("; ".join(errors))
        if url_elicitations:
            return self._lifecycle_output(
                ok=False,
                server=server_name,
                action="connect",
                prior_status=prior_status,
                message="URL-mode elicitation required.",
                errors=["URL-mode elicitation required."],
                auth_state="elicitation_required",
                next_step=url_elicitations[0].next_step,
                url_elicitations=url_elicitations,
            )
        auth_challenge = self._auth_challenge_from_message("; ".join(errors))
        auth_state = "none"
        if auth_challenge:
            auth_state = (
                "insufficient_scope"
                if auth_challenge.missing_scopes
                else "missing_auth"
            )
        return self._lifecycle_output(
            ok=not errors,
            server=server_name,
            action="connect",
            prior_status=prior_status,
            message=f"Server '{server_name}' connected."
            if not errors
            else f"Server '{server_name}' could not be connected.",
            errors=errors or None,
            auth_state=auth_state,
            auth_event=self._auth_event_for_challenge(auth_challenge),
            auth_challenge=auth_challenge,
            next_step=(
                "Resolve remote authorization out of band, then retry connection."
                if auth_challenge
                else None
            ),
        )

    async def disconnect_server(
        self, input_data: dict[str, Any]
    ) -> LifecycleServerOutput:
        """gateway.disconnect_server - Disconnect one known server."""
        parsed = DisconnectServerInput.model_validate(input_data)
        server_name = parsed.server_name
        prior_status = self._status_value(server_name)

        _config, failure = self._resolve_lifecycle_config(
            server_name, action="disconnect", prior_status=prior_status
        )
        if failure is not None:
            return failure

        active_tasks_before = len(self._client_manager.get_active_tasks(server_name))
        disconnected, cancelled, error = await self._client_manager.disconnect_server(
            server_name, force=parsed.force
        )
        active_tasks_after = len(self._client_manager.get_active_tasks(server_name))
        cancelled_tasks = max(active_tasks_before - active_tasks_after, 0)
        if not disconnected:
            return self._lifecycle_output(
                ok=False,
                server=server_name,
                action="disconnect",
                prior_status=prior_status,
                cancelled_request_count=cancelled,
                active_task_count=active_tasks_after,
                cancelled_task_count=cancelled_tasks,
                message=error or f"Server '{server_name}' could not be disconnected.",
                errors=[error] if error else None,
            )

        return self._lifecycle_output(
            ok=True,
            server=server_name,
            action="disconnect",
            prior_status=prior_status,
            cancelled_request_count=cancelled,
            active_task_count=active_tasks_after,
            cancelled_task_count=cancelled_tasks,
            message=f"Server '{server_name}' disconnected.",
        )

    async def restart_server(self, input_data: dict[str, Any]) -> LifecycleServerOutput:
        """gateway.restart_server - Restart one known server."""
        parsed = RestartServerInput.model_validate(input_data)
        server_name = parsed.server_name
        prior_status = self._status_value(server_name)

        config, failure = self._resolve_lifecycle_config(
            server_name, action="restart", prior_status=prior_status
        )
        if failure is not None:
            return failure
        if config is None:
            return self._lifecycle_output(
                ok=False,
                server=server_name,
                action="restart",
                prior_status=prior_status,
                message=f"Server '{server_name}' could not be resolved.",
                errors=[f"Unable to resolve server: {server_name}"],
            )

        return await self._restart_resolved_server(
            server_name, config, force=parsed.force, prior_status=prior_status
        )

    async def _restart_resolved_server(
        self,
        server_name: str,
        config: ResolvedServerConfig,
        *,
        force: bool,
        prior_status: str,
    ) -> LifecycleServerOutput:
        """Restart a server whose config the caller has ALREADY resolved.

        Split out of ``restart_server`` so ``update_server`` can restart onto
        the exact config object it probed and verified, instead of triggering a
        second independent resolve across the probe's 60-second await
        (Consiliency/pmcp#151).

        Every safety check -- policy, remote-header env vars, credential gaps --
        lives in ``_resolve_lifecycle_config``, which the caller runs to obtain
        ``config``. This helper deliberately holds none of them, so calling it
        with an unresolved or unverified config would bypass all three.
        """
        active_tasks_before = len(self._client_manager.get_active_tasks(server_name))
        ok, cancelled, errors = await self._client_manager.restart_server(
            config, force=force
        )
        active_tasks_after = len(self._client_manager.get_active_tasks(server_name))
        return self._lifecycle_output(
            ok=ok,
            server=server_name,
            action="restart",
            prior_status=prior_status,
            cancelled_request_count=cancelled,
            active_task_count=active_tasks_after,
            cancelled_task_count=max(active_tasks_before - active_tasks_after, 0),
            message=f"Server '{server_name}' restarted."
            if ok
            else f"Server '{server_name}' could not be restarted.",
            errors=errors or None,
        )

    def _check_api_key_available(self, env_var: str | None) -> bool:
        """Check if an API key is available in environment or any pmcp env store.

        This answers a boolean question by loading whole files into the
        gateway's ``os.environ`` -- every key in them, not just the one asked
        about. The load stays exactly as it was (so this predicate's answer is
        bit-for-bit unchanged, interpolation and ``override=False`` precedence
        included); what is new is that the keys each load INTRODUCES are
        recorded, so ``sanitized_subprocess_env`` keeps them out of the servers
        PMCP spawns (Consiliency/pmcp#229). Recording the delta rather than the
        file's contents is what makes that strip safe -- see
        ``env_store.record_dotenv_keys``.
        """
        if not env_var:
            return False

        # Check live environment first (fast path)
        if os.environ.get(env_var):
            return True

        # Check all env stores in priority order: project-local .env, then pmcp files
        for env_path in [
            Path.cwd() / ".env",
            Path.cwd() / ".env.pmcp",
            Path.home() / ".config" / "pmcp" / "pmcp.env",
        ]:
            if env_path.exists():
                before = set(os.environ)
                load_dotenv(env_path)
                record_dotenv_keys(set(os.environ) - before)
                if os.environ.get(env_var):
                    return True

        return False

    def _check_any_api_key_available(self, env_vars: list[str]) -> bool:
        """Check if any auth env var is available."""
        return any(self._check_api_key_available(env_var) for env_var in env_vars)

    def _auth_env_options(self, server_name: str, env_var: str | None) -> list[str]:
        """Return acceptable secret-store keys for a server.

        Includes the server's namespaced storage key (``secret_key``) ahead of
        the runtime ``env_var`` legacy fallback so availability checks stay
        correct after a credential is migrated to the namespaced key.
        """
        options: list[str] = []
        manifest_server = load_manifest().get_server(server_name)
        for key in credential_lookup_keys(manifest_server):
            if key not in options:
                options.append(key)
        if env_var and env_var not in options:
            options.append(env_var)
        # Browser Use supports either OpenAI or Anthropic provider credentials.
        if server_name == "browser-use":
            for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
                if key not in options:
                    options.append(key)
        return options

    def _auth_methods_for_server(self, server_name: str) -> list[str]:
        """Return supported auth methods for UX hints."""
        if server_name == "browser-use":
            return ["api_key", "subscription_token"]
        return ["api_key"]

    def _configured_duplicate_missing_credential(
        self, server_name: str, configured: ResolvedServerConfig
    ) -> tuple[ServerConfig, list[str]] | None:
        """Credential gate for a *configured* (.mcp.json) entry that also
        matches a manifest server.

        A `.mcp.json` entry duplicating a manifest server was previously
        never credential-checked at all here — `manifest_server` was always
        `None` for this path, so a genuinely-required server with no
        credential reached `ensure_connected`/`connect_server` unauthenticated
        (Consiliency/pmcp#114 board review finding 1). Returns
        `(manifest_server, auth_env_options)` when the credential is still
        required and unavailable, else `None`.
        """
        manifest_server = load_manifest().get_server(server_name)
        if not manifest_server or not manifest_server.env_var:
            return None

        if isinstance(configured.config, RemoteMcpServerConfig):
            # extra_env is carried to a spawned local subprocess's env; a
            # remote connection has no such subprocess, so a relaxer can
            # never actually reach it no matter what the manifest declares
            # (board review finding 1, remote-configured-duplicate variant:
            # loader.py's "never relax a remote server" clause checks the
            # MANIFEST server's url, which can be None while the CONFIGURED
            # duplicate is itself remote — that combination must still fail
            # closed). Judge only the declared requirement, ignoring any
            # relaxer.
            required = bool(manifest_server.requires_api_key)
        else:
            child_env = (
                configured.config.env
                if isinstance(configured.config, LocalMcpServerConfig)
                else None
            )
            required = requires_credential(manifest_server, child_env=child_env)

        if not required:
            return None

        auth_env_options = self._auth_env_options(server_name, manifest_server.env_var)
        if self._check_any_api_key_available(auth_env_options):
            return None

        # _check_any_api_key_available only checks process env and the PMCP
        # secret stores BY VARIABLE NAME — it cannot see a concrete
        # credential the user wrote directly into the configured entry's own
        # `env` block (board review finding 2). An empty string or
        # unexpanded `${VAR}` placeholder does NOT satisfy it — same
        # usability rule as a relaxer value.
        if (
            isinstance(configured.config, LocalMcpServerConfig)
            and configured.config.env
        ):
            for key in auth_env_options:
                if is_usable_credential_value(configured.config.env.get(key)):
                    return None

        return (manifest_server, auth_env_options)

    def _write_secret(self, scope: str, key: str, value: str) -> Path:
        """Persist a secret in PMCP env storage."""
        return set_env_value(scope, key, value, self._project_root)

    def _normalize_token(self, value: str) -> str:
        """Normalize user query tokens for matching/discovery."""
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    async def _load_registry_candidates(self) -> RegistryCache:
        """Load the registry cache, falling back to a bounded live fetch."""
        cached = load_registry_cache()
        if cached is not None and cached.servers:
            return cached
        # The private-registry opt-in has two sources (#139): the env var and
        # the `allowPrivateRegistry` config field. Resolve the config half here,
        # where the config paths are known, rather than reaching for global
        # state inside registry.py.
        endpoint = effective_registry_endpoint(
            config_value=registry_allow_private_from_config(
                project_root=self._project_root,
                custom_config_path=self._custom_config_path,
            )
        )
        # Draft-schema tolerance applies only when actually using a private
        # endpoint, so enabling the flag never changes public-registry results.
        fetched = await fetch_registry_servers(
            endpoint,
            allow_draft_schema=endpoint != DEFAULT_REGISTRY_ENDPOINT,
        )
        if fetched.servers:
            return fetched
        return cached or fetched

    async def _registry_matches(
        self, query: str, *, limit: int = 8
    ) -> list[RegistryServerEntry]:
        normalized = self._normalize_token(query)
        if not normalized:
            return []
        query_words = set(normalized.split())
        scored: list[tuple[int, str, RegistryServerEntry]] = []
        for entry in (await self._load_registry_candidates()).servers:
            package_text = " ".join(pkg.identifier for pkg in entry.packages)
            remote_text = " ".join(remote.url or "" for remote in entry.remotes)
            haystack = self._normalize_token(
                " ".join(
                    [
                        entry.name,
                        entry.description,
                        package_text,
                        remote_text,
                        " ".join(entry.declared_capabilities),
                    ]
                )
            )
            hay_words = set(haystack.split())
            score = len(query_words & hay_words)
            if normalized in haystack:
                score += 3
            if score:
                scored.append((score, entry.name, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [entry for _, _, entry in scored[:limit]]

    def _registry_candidate_for_entry(
        self, entry: RegistryServerEntry, *, score: float = 0.7
    ) -> CapabilityCandidate | None:
        package = entry.packages[0] if entry.packages else None
        remote = entry.remotes[0] if entry.remotes else None
        if package is None and remote is None:
            return None
        if not self._policy_manager.is_server_allowed(entry.name):
            return None
        env_vars = package.env_vars if package is not None else []
        remote_headers = remote.headers if remote is not None else []
        auth_vars = env_vars or remote_headers
        env_var = auth_vars[0] if auth_vars else None
        # Include the server's namespaced storage key (when the registry entry
        # name matches a manifest server declaring a secret_key) so a credential
        # stored under the namespaced key reads as available, not "api key needed".
        availability_keys = list(auth_vars)
        for key in self._auth_env_options(entry.name, env_var):
            if key not in availability_keys:
                availability_keys.append(key)
        return CapabilityCandidate(
            name=entry.name,
            candidate_type="server",
            relevance_score=score,
            reasoning=entry.description or "MCP Registry candidate",
            requires_api_key=bool(auth_vars),
            api_key_available=self._check_any_api_key_available(availability_keys),
            env_var=env_var,
            is_running=False,
            source="registry",
            transport=(
                remote.transport
                if remote is not None
                else package.transport
                if package is not None
                else None
            ),
            url=(
                remote.url
                if remote is not None
                else package.url
                if package is not None
                else None
            ),
            remote_headers=remote_headers,
            package=package.identifier if package is not None else None,
            server_card_url=entry.server_card_url,
            protected_resource_metadata_url=entry.protected_resource_metadata_url,
            authorization_server_metadata_url=entry.authorization_server_metadata_url,
            declared_scopes=entry.declared_scopes,
            declared_capabilities=entry.declared_capabilities,
        )

    async def _registry_candidates_for_query(
        self, query: str, *, limit: int = 5
    ) -> list[CapabilityCandidate]:
        candidates: list[CapabilityCandidate] = []
        for entry in await self._registry_matches(query, limit=limit):
            candidate = self._registry_candidate_for_entry(entry)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _load_configured_servers(self) -> dict[str, ResolvedServerConfig]:
        """Load user/project-configured servers that are allowed by policy."""
        configs = load_configs(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )
        configs = filter_self_references(configs)

        configured: dict[str, ResolvedServerConfig] = {}
        for config in configs:
            if self._policy_manager.is_server_allowed(config.name):
                configured[config.name] = config
        return configured

    def _load_all_configured_servers(self) -> dict[str, ResolvedServerConfig]:
        """Load user/project-configured servers before policy filtering."""
        configs = load_configs(
            project_root=self._project_root,
            custom_config_path=self._custom_config_path,
        )
        return {config.name: config for config in filter_self_references(configs)}

    def _status_value(self, server_name: str) -> str:
        """Return a public status string for a server, or unknown."""
        status = self._client_manager.get_server_status(server_name)
        if status is None:
            return "unknown"
        return status.status.value

    def _protocol_version(self, server_name: str | None) -> str | None:
        if server_name is None:
            return None
        status = self._client_manager.get_server_status(server_name)
        protocol_version = getattr(status, "protocol_version", None) if status else None
        return protocol_version if isinstance(protocol_version, str) else None

    def _lifecycle_output(
        self,
        *,
        ok: bool,
        server: str,
        action: Literal["connect", "disconnect", "restart"],
        prior_status: str,
        message: str,
        cancelled_request_count: int = 0,
        active_task_count: int = 0,
        cancelled_task_count: int = 0,
        errors: list[str] | None = None,
        missing_env_vars: list[str] | None = None,
        auth_state: str = "none",
        auth_event: AuthEventKind | None = None,
        next_step: str | None = None,
        auth_methods: list[str] | None = None,
        auth_metadata: Any | None = None,
        auth_challenge: Any | None = None,
        url_elicitations: list[Any] | None = None,
    ) -> LifecycleServerOutput:
        self._audit(
            method=f"gateway.{action}_server",
            action=action,
            outcome="success" if ok else "refused",
            started_at=time.monotonic(),
            server_name=server,
            protocol_version=self._protocol_version(server),
            auth_state=auth_state,
            auth_event=auth_event,
            error="; ".join(errors) if errors else None,
        )
        return LifecycleServerOutput(
            ok=ok,
            server=server,
            action=action,
            prior_status=prior_status,
            new_status=self._status_value(server),
            cancelled_request_count=cancelled_request_count,
            active_task_count=active_task_count,
            cancelled_task_count=cancelled_task_count,
            message=message,
            errors=errors,
            missing_env_vars=missing_env_vars or [],
            auth_state=cast(Any, auth_state),
            next_step=next_step,
            auth_methods=auth_methods,
            auth_metadata=auth_metadata,
            auth_challenge=auth_challenge,
            url_elicitations=url_elicitations,
        )

    def _missing_remote_header_env_vars(
        self,
        config: ResolvedServerConfig,
    ) -> list[str]:
        if not isinstance(config.config, RemoteMcpServerConfig):
            return []
        resolution = resolve_remote_headers(
            config.config.headers,
            build_remote_header_env_lookup(self._project_root),
        )
        return resolution.missing_env_vars

    def _remote_header_missing_lifecycle_output(
        self,
        config: ResolvedServerConfig,
        *,
        action: Literal["connect", "disconnect", "restart"],
        prior_status: str,
        missing_env_vars: list[str],
    ) -> LifecycleServerOutput:
        env_names = ", ".join(missing_env_vars)
        return self._lifecycle_output(
            ok=False,
            server=config.name,
            action=action,
            prior_status=prior_status,
            message=(
                f"Server '{config.name}' requires remote header authentication. "
                f"Set missing environment variable(s): {env_names}"
            ),
            errors=[
                f"Missing remote header environment variable(s) for '{config.name}': {env_names}"
            ],
            missing_env_vars=missing_env_vars,
            auth_state="missing_auth",
            auth_event="missing_credential",
            auth_methods=self._auth_methods_for_server(config.name),
            next_step=f"gateway.auth_connect(server_name='{config.name}')",
        )

    def _resolve_lifecycle_config(
        self,
        server_name: str,
        *,
        action: Literal["connect", "disconnect", "restart"],
        prior_status: str,
    ) -> tuple[ResolvedServerConfig | None, LifecycleServerOutput | None]:
        """Resolve a lifecycle target or return a structured failure output."""
        configured_servers = self._load_all_configured_servers()
        if server_name in configured_servers:
            if not self._policy_manager.is_server_allowed(server_name):
                return (
                    None,
                    self._lifecycle_output(
                        ok=False,
                        server=server_name,
                        action=action,
                        prior_status=prior_status,
                        message=f"Server '{server_name}' is blocked by policy.",
                        errors=[f"Server '{server_name}' is blocked by policy."],
                        auth_state="policy_denied",
                    ),
                )
            configured = configured_servers[server_name]
            missing_env_vars = self._missing_remote_header_env_vars(configured)
            if missing_env_vars:
                return (
                    None,
                    self._remote_header_missing_lifecycle_output(
                        configured,
                        action=action,
                        prior_status=prior_status,
                        missing_env_vars=missing_env_vars,
                    ),
                )
            # Same class of bug as the provision() and startup-resolution
            # fixes (Consiliency/pmcp#114 board review finding 1): a
            # configured duplicate of a manifest server was never
            # credential-gated here at all.
            credential_gap = self._configured_duplicate_missing_credential(
                server_name, configured
            )
            if credential_gap:
                manifest_server, auth_env_options = credential_gap
                env_names = ", ".join(auth_env_options)
                return (
                    None,
                    self._lifecycle_output(
                        ok=False,
                        server=server_name,
                        action=action,
                        prior_status=prior_status,
                        message=(
                            f"Server '{server_name}' requires authentication. "
                            f"Set one of: {env_names}"
                        ),
                        errors=[
                            f"Missing authentication environment variable for '{server_name}': {env_names}"
                        ],
                        missing_env_vars=auth_env_options,
                        auth_state="missing_auth",
                        auth_event="missing_credential",
                        auth_methods=self._auth_methods_for_server(server_name),
                        auth_metadata=self._auth_metadata_for_server(manifest_server),
                        next_step=f"gateway.auth_connect(server_name='{server_name}')",
                    ),
                )
            return (configured, None)

        manifest = load_manifest()
        server_config = manifest.get_server(server_name)
        if server_config is None:
            server_config = self._discovered_server_configs.get(server_name)

        if server_config is not None:
            if not self._policy_manager.is_server_allowed(server_name):
                return (
                    None,
                    self._lifecycle_output(
                        ok=False,
                        server=server_name,
                        action=action,
                        prior_status=prior_status,
                        message=f"Server '{server_name}' is blocked by policy.",
                        errors=[f"Server '{server_name}' is blocked by policy."],
                        auth_state="policy_denied",
                    ),
                )

            if requires_credential(server_config) and server_config.env_var:
                auth_env_options = self._auth_env_options(
                    server_name, server_config.env_var
                )
                if not self._check_any_api_key_available(auth_env_options):
                    env_names = ", ".join(auth_env_options)
                    return (
                        None,
                        self._lifecycle_output(
                            ok=False,
                            server=server_name,
                            action=action,
                            prior_status=prior_status,
                            message=(
                                f"Server '{server_name}' requires authentication. "
                                f"Set one of: {env_names}"
                            ),
                            errors=[
                                f"Missing authentication environment variable for '{server_name}': {env_names}"
                            ],
                            missing_env_vars=auth_env_options,
                            auth_state="missing_auth",
                            auth_event="missing_credential",
                            auth_methods=self._auth_methods_for_server(server_name),
                            auth_metadata=self._auth_metadata_for_server(server_config),
                            next_step=f"gateway.auth_connect(server_name='{server_name}')",
                        ),
                    )

            resolved = manifest_server_to_config(server_config)
            missing_env_vars = self._missing_remote_header_env_vars(resolved)
            if missing_env_vars:
                return (
                    None,
                    self._remote_header_missing_lifecycle_output(
                        resolved,
                        action=action,
                        prior_status=prior_status,
                        missing_env_vars=missing_env_vars,
                    ),
                )

            return (resolved, None)

        if action == "disconnect" and self._client_manager.get_server_status(
            server_name
        ):
            return (
                ResolvedServerConfig(
                    name=server_name,
                    source="custom",
                    config=LocalMcpServerConfig(command=""),
                ),
                None,
            )

        return (
            None,
            self._lifecycle_output(
                ok=False,
                server=server_name,
                action=action,
                prior_status=prior_status,
                message=f"Server '{server_name}' is not known to PMCP.",
                errors=[f"Unknown server: {server_name}"],
            ),
        )

    def _keywords_for_config_server(self, config: ResolvedServerConfig) -> list[str]:
        """Build lightweight keywords for a configured server entry."""
        keywords: list[str] = [config.name, "mcp", "server"]
        name_words = config.name.replace("-", " ").replace("_", " ").split()
        keywords.extend(name_words)
        if name_words:
            keywords.append(" ".join(name_words))
        if " ".join(name_words).lower() == "tenant code mode":
            keywords.extend(
                [
                    "code execution",
                    "sandbox execution",
                    "mobile code mode",
                    "task runs",
                    "logs",
                    "artifacts",
                    "hosted sandbox",
                ]
            )
        if isinstance(config.config, LocalMcpServerConfig):
            if config.config.command:
                keywords.append(config.config.command)
            keywords.extend(config.config.args)
        elif isinstance(config.config, RemoteMcpServerConfig):
            keywords.extend(["remote", "sse", "http", "api"])
            keywords.append(config.config.url)

        # Deduplicate while preserving order
        deduped: list[str] = []
        seen: set[str] = set()
        for keyword in keywords:
            keyword_str = str(keyword).strip().lower()
            if keyword_str and keyword_str not in seen:
                seen.add(keyword_str)
                deduped.append(keyword_str)
        return deduped

    def _build_manifest_with_config_servers(
        self,
        manifest: Manifest,
        configured_servers: dict[str, ResolvedServerConfig],
    ) -> Manifest:
        """Create a manifest view that includes config-only server entries."""
        merged_servers = dict(manifest.servers)

        for name, config in configured_servers.items():
            if name in merged_servers:
                continue

            command = ""
            args: list[str] = []
            description = f"User-configured MCP server '{name}'"

            if isinstance(config.config, LocalMcpServerConfig):
                command = config.config.command
                args = list(config.config.args)
                if command:
                    description += f" (local command: {command})"
            elif isinstance(config.config, RemoteMcpServerConfig):
                description += f" (remote URL: {config.config.url})"

            merged_servers[name] = ServerConfig(
                name=name,
                description=description,
                keywords=self._keywords_for_config_server(config),
                install={},
                command=command,
                args=args,
                requires_api_key=False,
                transport=config.config.type
                if isinstance(config.config, RemoteMcpServerConfig)
                else "local",
                url=config.config.url
                if isinstance(config.config, RemoteMcpServerConfig)
                else None,
                headers=config.config.headers
                if isinstance(config.config, RemoteMcpServerConfig)
                else None,
            )

        return Manifest(
            version=manifest.version,
            cli_alternatives=dict(manifest.cli_alternatives),
            servers=merged_servers,
            discovery_queue_path=manifest.discovery_queue_path,
        )

    def _get_server_env_metadata(
        self,
        server_name: str,
        manifest: Manifest,
        configured_servers: dict[str, ResolvedServerConfig],
    ) -> tuple[bool, str | None, str | None]:
        """Get API-key metadata for a server candidate.

        The first element is the *effective* requirement
        (``requires_credential``), not the raw declared ``requires_api_key`` —
        a relaxed server reports no key required here even though it still
        carries ``env_var``/``env_instructions`` unchanged, so
        ``gateway.auth_connect`` still works for an operator who later wants a
        real key (IF-0-P5-3).

        A server name present in *configured_servers* is a configured
        duplicate: the child env it will actually receive is that entry's
        ``config.env``, not the manifest's bare ``extra_env`` — passed as
        ``child_env`` so an override (e.g. an empty or placeholder value in
        ``.mcp.json``) is judged correctly instead of always reading the
        manifest default (Consiliency/pmcp#114 board review finding 1). A
        configured duplicate that is itself REMOTE never relaxes, regardless
        of the manifest's own transport — extra_env cannot reach an HTTP
        connection, so relaxation is judged on the declared requirement only
        (board review finding 1, remote variant).
        """
        manifest_server = manifest.get_server(server_name)
        if manifest_server:
            configured = configured_servers.get(server_name)
            if configured is not None and isinstance(
                configured.config, RemoteMcpServerConfig
            ):
                requires_api_key = bool(manifest_server.requires_api_key)
            else:
                child_env = (
                    configured.config.env
                    if configured is not None
                    and isinstance(configured.config, LocalMcpServerConfig)
                    else None
                )
                requires_api_key = requires_credential(
                    manifest_server, child_env=child_env
                )
            return (
                requires_api_key,
                manifest_server.env_var,
                manifest_server.env_instructions,
            )

        # No explicit API key metadata for plain .mcp.json entries.
        return (False, None, None)

    def _get_server_config_for_update(self, server_name: str) -> ServerConfig | None:
        """Resolve server config from manifest or discovered candidates."""
        manifest = load_manifest()
        server_config = manifest.get_server(server_name)
        if server_config:
            return server_config
        return self._discovered_server_configs.get(server_name)

    async def _run_update_probe_command(
        self, command: list[str], env: dict[str, str] | None = None
    ) -> tuple[bool, str]:
        """Run an update probe command and return (ok, output).

        ``env`` should be sanitized (this probe runs the downstream server's own
        package code, e.g. ``npx <pkg> --help``, so it must not inherit other
        servers' credentials from the gateway's environment).
        """
        # start_new_session makes the child a process-group leader, so a probe
        # that hangs can be reaped as a TREE. Without it the timeout below
        # abandoned the child: `wait_for` cancels the `communicate()` await but
        # never signals the process, so a probe like `npx <pkg> --help` against a
        # package that ignores the flag and runs as a server left its whole
        # process tree alive -- including grandchildren such as the Chrome that
        # @playwright/mcp launches, which then holds the profile SingletonLock
        # and breaks the next launch.
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
        except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
            # Reap the tree before propagating; the caller turns TimeoutError
            # into a user-facing "probe timed out" result, and cancellation must
            # not leak a process either.
            #
            # asyncio.TimeoutError is listed EXPLICITLY: it only became an alias
            # of the builtin TimeoutError in 3.11, and this project supports
            # 3.10, where catching the builtin alone lets the timeout escape and
            # the cleanup never runs. CI caught this on 3.10 while 3.11 and 3.12
            # both passed -- the fix silently did nothing on the oldest
            # supported version.
            await _terminate_process_tree(process, "update-probe")
            raise
        output = (
            stdout.decode("utf-8", errors="replace")
            + "\n"
            + stderr.decode("utf-8", errors="replace")
        ).strip()
        return (process.returncode == 0, output)

    def _telemetry_enabled(self) -> bool:
        """Whether feedback telemetry workflow is enabled."""
        if self._guidance_config is None:
            return True
        return self._guidance_config.enable_telemetry

    def _feedback_hint(self) -> str | None:
        """Guidance for agentic frameworks on feedback submission flow."""
        if not self._telemetry_enabled():
            return None
        return (
            "Technical failure detected. If your framework supports ask-user/question tools, "
            "ask consent before submission. Then call gateway.submit_feedback with the exact "
            "title/description payload you will send and show it verbatim to the user first. "
            "Warning: submission may use model tokens and sends technical data to GitHub. "
            "Do not include personal data, credentials, or secrets."
        )

    def _record_feedback_event(self, event_type: str, details: dict[str, Any]) -> None:
        """Record compact event history for feedback telemetry context."""

        def sanitize_value(value: Any) -> Any:
            if isinstance(value, str):
                return sanitize_auth_diagnostic(value)
            if isinstance(value, dict):
                return {str(k): sanitize_value(v) for k, v in value.items()}
            if isinstance(value, list):
                return [sanitize_value(v) for v in value]
            return value

        event = {
            "ts": int(time.time()),
            "event_type": event_type,
            "details": sanitize_value(details),
        }
        self._feedback_events.append(event)
        if len(self._feedback_events) > 12:
            self._feedback_events = self._feedback_events[-12:]

    def _truncate_token_text(
        self, text: str, token_limit: int = FEEDBACK_TOKEN_LIMIT
    ) -> str:
        """Approximate token truncation using whitespace-delimited tokens."""
        tokens = text.split()
        if len(tokens) <= token_limit:
            return text
        return " ".join(tokens[:token_limit])

    def _scrub_sensitive_text(self, text: str) -> str:
        """Remove obvious secrets and personal identifiers from feedback text."""
        scrubbed = self._policy_manager.redact_secrets(text)
        return re.sub(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            "[REDACTED_EMAIL]",
            scrubbed,
        )

    def _build_feedback_issue(
        self,
        parsed: SubmitFeedbackInput,
        issue_repo: str,
    ) -> tuple[str, str]:
        """Build issue title/body with telemetry template."""
        safe_description = self._truncate_token_text(
            self._scrub_sensitive_text(parsed.description)
        )
        events_json = json.dumps(
            {
                "feedback_events": self._feedback_events[-6:],
                "audit_events": [
                    event.model_dump(mode="json", exclude_none=True)
                    for event in list(self._audit_events)[-6:]
                ],
            },
            indent=2,
        )
        # All caller-supplied fields land in a potentially public GitHub issue,
        # so scrub each the same way as the description — not just description.
        subordinate = (
            self._scrub_sensitive_text(parsed.subordinate_server)
            if parsed.subordinate_server
            else "unknown"
        )
        failed_call = (
            self._scrub_sensitive_text(parsed.failed_tool_call)
            if parsed.failed_tool_call
            else "unknown"
        )

        title_prefix = (
            "[Feedback][Bug]" if parsed.issue_type == "bug" else "[Feedback][Feature]"
        )
        issue_title = (
            f"{title_prefix} {self._scrub_sensitive_text(parsed.title.strip())}"
        )
        issue_title = issue_title[:160]

        body = (
            "## Privacy Notice\n"
            "- Technical data only. No personal data should be included.\n"
            "- Data is submitted to GitHub and may be publicly visible depending on repository settings.\n"
            "- Payload is limited to approximately 4000 tokens.\n\n"
            "## User Report\n"
            f"{safe_description}\n\n"
            "## Telemetry\n"
            f"- pmcp_version: {PMCP_VERSION}\n"
            f"- platform: {platform.platform()}\n"
            f"- python_version: {platform.python_version()}\n"
            f"- subordinate_server: {subordinate}\n"
            f"- failed_tool_call: {failed_call}\n"
            f"- target_repository: {issue_repo}\n\n"
            "## Recent PMCP Events\n"
            "```json\n"
            f"{events_json}\n"
            "```\n"
        )
        return issue_title, body

    async def request_capability(
        self, input_data: dict[str, Any]
    ) -> CapabilityResolution:
        """gateway.request_capability - Request a capability by natural language.

        Returns ranked candidates for Claude Code to choose from.
        Use gateway.provision to actually install/start the chosen server.
        """
        parsed = CapabilityRequestInput.model_validate(input_data)
        self._record_feedback_event("capability_request", {"query": parsed.query})

        # Load manifest
        manifest = load_manifest()
        configured_servers = self._load_configured_servers()

        merged_manifest = self._build_manifest_with_config_servers(
            manifest, configured_servers
        )

        # Get detected CLIs (from input, cached probe metadata, or a bounded probe).
        detected_clis, detected_cli_infos = await self._resolve_cli_availability(
            manifest,
            explicit_available_clis=parsed.available_clis,
        )
        cli_hint_matches = rank_cli_hints(
            parsed.query,
            manifest,
            available_clis=detected_clis,
            detected_cli_infos=detected_cli_infos,
        )
        if cli_hint_matches:
            logger.debug(
                "Matched CLI hints for future response plumbing: %s",
                ", ".join(match.hint.name for match in cli_hint_matches[:3]),
            )
        cli_hint_match = next(
            (match for match in cli_hint_matches if match.hint.available),
            None,
        )

        # Get running servers
        running_servers = [
            s.name
            for s in self._client_manager.get_all_server_statuses()
            if s.status.value == "online"
        ]

        # --- Tier 1: explicit server name match ---
        # Match by normalizing server names and query word groups (strips hyphens,
        # underscores, spaces) so "bright data" → "brightdata", "brave-search" →
        # "bravesearch", etc. Does NOT use keywords to avoid matching generic
        # capability words like "browser" or "search".
        query_lower = parsed.query.lower()
        query_words = query_lower.split()
        norm_to_server = {
            n.lower().replace("-", "").replace("_", "").replace(" ", ""): n
            for n in merged_manifest.servers
        }
        name_match: str | None = None
        for window_size in (3, 2, 1):
            for i in range(len(query_words) - window_size + 1):
                window = (
                    "".join(query_words[i : i + window_size])
                    .replace("-", "")
                    .replace("_", "")
                )
                if window in norm_to_server:
                    name_match = norm_to_server[window]
                    break
            if name_match:
                break

        explicit_mcp_intent = any(
            word in query_words
            for word in ("mcp", "server", "servers", "provision", "install", "start")
        )
        name_match_collides_with_cli = (
            cli_hint_match is not None and cli_hint_match.hint.name == name_match
        )

        if (
            name_match
            and self._policy_manager.is_server_allowed(name_match)
            and (explicit_mcp_intent or not name_match_collides_with_cli)
        ):
            requires_api_key, env_var, env_instructions = self._get_server_env_metadata(
                name_match, manifest, configured_servers
            )
            api_key_available = self._check_any_api_key_available(
                self._auth_env_options(name_match, env_var)
            )
            candidate = CapabilityCandidate(
                name=name_match,
                candidate_type="server",
                relevance_score=1.0,
                reasoning=f"Explicit name match for '{name_match}' in query.",
                requires_api_key=requires_api_key,
                api_key_available=api_key_available,
                env_var=env_var,
                env_instructions=env_instructions,
                is_running=name_match in running_servers,
            )
            msg = f"Matched '{name_match}' by name."
            if requires_api_key:
                if api_key_available:
                    msg += f" API key ({env_var}) is already set — ready to provision."
                else:
                    msg += (
                        f" Requires API key ({env_var}). "
                        f"Set it or call gateway.auth_connect, then gateway.provision."
                    )
            else:
                msg += " No API key required. Call gateway.provision to install."
            return CapabilityResolution(
                status="candidates",
                message=msg,
                candidates=[candidate],
                recommendation=f"Call gateway.provision(server_name='{name_match}')",
            )

        if cli_hint_match is not None:
            hint = cli_hint_match.hint
            return CapabilityResolution(
                status="use_cli",
                message=(
                    f"Use Bash/direct CLI with '{hint.name}'. PMCP is recommending "
                    "the native command here; it is not executing the command or "
                    "provisioning an MCP server for this path."
                ),
                cli=CLIResolution(
                    name=hint.name,
                    path=hint.path,
                    description=hint.description,
                    available=hint.available,
                    check_command=hint.check_command,
                    help_command=hint.help_command,
                    examples=hint.examples,
                    prefer_mcp_for=hint.prefer_mcp_for,
                    reason=hint.reason,
                ),
                recommendation=(
                    f"Run '{hint.name}' directly via Bash/direct CLI. "
                    "Use gateway.request_capability again only if you need an MCP server."
                ),
            )

        # --- Tier 2 pre-check: detect unknown named services (Fix C, issue #56) ---
        # If the query contains a PascalCase word (not the first word of the sentence)
        # that is NOT a known server name, the user is likely requesting a specific
        # external service not in the manifest. Skip category matching and fall
        # through to not_available so search_registry guidance is surfaced.
        _pascal_re = re.compile(r"^[A-Z][a-z]{3,}$")
        _unknown_service = any(
            idx > 0
            and _pascal_re.match(w)
            and w.lower().replace("-", "").replace("_", "") not in norm_to_server
            for idx, w in enumerate(parsed.query.split())
        )

        # --- Tier 2: category keyword match ---
        category_result = (
            None if _unknown_service else manifest.get_servers_in_category(parsed.query)
        )
        all_candidates: list[CapabilityCandidate] = []
        if category_result:
            cat_name, cat_servers = category_result

            # Build enriched candidates for every server in the category
            for scfg in cat_servers:
                if not self._policy_manager.is_server_allowed(scfg.name):
                    continue
                requires_api_key, env_var, env_instructions = (
                    self._get_server_env_metadata(
                        scfg.name, manifest, configured_servers
                    )
                )
                api_key_available = self._check_any_api_key_available(
                    self._auth_env_options(scfg.name, env_var)
                )
                all_candidates.append(
                    CapabilityCandidate(
                        name=scfg.name,
                        candidate_type="server",
                        relevance_score=1.0,
                        reasoning=scfg.description,
                        requires_api_key=requires_api_key,
                        api_key_available=api_key_available,
                        env_var=env_var,
                        env_instructions=env_instructions,
                        is_running=scfg.name in running_servers,
                    )
                )

            if not all_candidates:
                category_result = None

        if category_result and all_candidates:
            cat_name, _cat_servers = category_result

            # Sort: no-key-required first, then key-available, then key-missing
            def _sort_key(c: CapabilityCandidate) -> int:
                if not c.requires_api_key:
                    return 0
                if c.api_key_available:
                    return 1
                return 2

            all_candidates.sort(key=_sort_key)

            # Build a human-readable message grouping the three tiers
            free = [c for c in all_candidates if not c.requires_api_key]
            key_ready = [
                c for c in all_candidates if c.requires_api_key and c.api_key_available
            ]
            key_missing = [
                c
                for c in all_candidates
                if c.requires_api_key and not c.api_key_available
            ]

            parts: list[str] = [
                f"{len(all_candidates)} options in '{cat_name}' category. "
                "Review and call gateway.provision(server_name='...') with your choice."
            ]
            if free:
                names = ", ".join(c.name for c in free)
                parts.append(f"No API key required: {names}.")
            if key_ready:
                names = ", ".join(f"{c.name} ({c.env_var} ✓)" for c in key_ready)
                parts.append(f"API key already set — ready to provision: {names}.")
            if key_missing:
                names = ", ".join(f"{c.name} (needs {c.env_var})" for c in key_missing)
                parts.append(
                    f"Requires API key (not set): {names}. "
                    "Provide the key or call gateway.auth_connect first."
                )

            return CapabilityResolution(
                status="pick_from_category",
                message=" ".join(parts),
                candidates=all_candidates,
                category_name=cat_name,
                recommendation=(
                    "Choose based on your needs and API key availability. "
                    "Call gateway.provision(server_name='<chosen>') to install."
                ),
            )

        query_norm = parsed.query.lower().replace("-", " ").replace("_", " ")
        configured_query_words = set(query_norm.split())
        generic_config_keywords = {"mcp", "server", "remote", "sse", "http", "api"}
        configured_keyword_matches: list[tuple[str, ServerConfig, list[str]]] = []
        for server_name in configured_servers:
            if not self._policy_manager.is_server_allowed(server_name):
                continue
            server_config = merged_manifest.servers.get(server_name)
            if not server_config:
                continue
            matched_keywords = []
            for keyword in server_config.keywords:
                keyword_norm = keyword.lower().replace("-", " ").replace("_", " ")
                keyword_words = set(keyword_norm.split())
                if (
                    keyword_norm in generic_config_keywords
                    or keyword_norm.startswith("http")
                    or not keyword_words
                ):
                    continue
                if keyword_norm in query_norm or keyword_words.issubset(
                    configured_query_words
                ):
                    matched_keywords.append(keyword)
            if matched_keywords:
                configured_keyword_matches.append(
                    (server_name, server_config, matched_keywords)
                )

        if configured_keyword_matches:
            configured_keyword_matches.sort(key=lambda item: (-len(item[2]), item[0]))
            server_name, _server_config, matched_keywords = configured_keyword_matches[
                0
            ]
            requires_api_key, env_var, env_instructions = self._get_server_env_metadata(
                server_name, manifest, configured_servers
            )
            api_key_available = self._check_any_api_key_available(
                self._auth_env_options(server_name, env_var)
            )
            return CapabilityResolution(
                status="candidates",
                message=(
                    f"Matched configured MCP server '{server_name}' by keywords. "
                    "Call gateway.provision to start it when needed."
                ),
                candidates=[
                    CapabilityCandidate(
                        name=server_name,
                        candidate_type="server",
                        relevance_score=min(1.0, len(matched_keywords) / 3),
                        reasoning=(
                            "Keyword match for configured server: "
                            f"{', '.join(matched_keywords[:3])}"
                        ),
                        requires_api_key=requires_api_key,
                        api_key_available=api_key_available,
                        env_var=env_var,
                        env_instructions=env_instructions,
                        is_running=server_name in running_servers,
                    )
                ],
                recommendation=f"Call gateway.provision(server_name='{server_name}')",
            )

        registry_candidates = (
            await self._registry_candidates_for_query(parsed.query)
            if _unknown_service
            else []
        )
        if registry_candidates:
            return CapabilityResolution(
                status="candidates",
                message=(
                    "Found MCP Registry candidates. PMCP will not install or connect "
                    "them until you explicitly choose and register one."
                ),
                candidates=registry_candidates,
                recommendation=(
                    "Review the registry metadata, then call "
                    "gateway.register_discovered_server for the selected package."
                ),
            )

        # --- Tier 3: no match ---
        logger.info(f"Unmatched capability request: {parsed.query}")
        self._record_feedback_event(
            "capability_unmatched", {"query": parsed.query, "path": "no_match"}
        )
        return CapabilityResolution(
            status="not_available",
            message=f"No matching capability found for: {parsed.query}",
            logged_for_discovery=True,
            search_guidance=(
                f'Call gateway.search_registry(query="{parsed.query}") '
                "to search the public MCP Registry for external servers. "
                "Then call gateway.register_discovered_server and gateway.provision to install."
            ),
        )

    async def provision(self, input_data: dict[str, Any]) -> ProvisionOutput:
        """gateway.provision - Start background installation of an MCP server."""
        parsed = ProvisionInput.model_validate(input_data)
        server_name = parsed.server_name
        self._record_feedback_event("provision_attempt", {"server": server_name})

        configured_servers = self._load_configured_servers()

        if not self._policy_manager.is_server_allowed(server_name):
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=f"Server '{server_name}' is blocked by policy.",
                auth_state="policy_denied",
                feedback_hint=self._feedback_hint(),
            )

        # Check if already running
        if self._client_manager.is_server_online(server_name):
            tools = [
                t
                for t in self._client_manager.get_all_tools()
                if t.server_name == server_name
            ]
            return ProvisionOutput(
                ok=True,
                server=server_name,
                status="already_running",
                message=f"Server '{server_name}' is already running with {len(tools)} tools.",
                new_tools=[
                    CapabilityCard(
                        tool_id=t.tool_id,
                        server=t.server_name,
                        tool_name=t.tool_name,
                        short_description=t.short_description,
                        tags=t.tags,
                        availability="online",
                        risk_hint=t.risk_hint.value,
                    )
                    for t in tools[:10]
                ],
                feedback_hint=None,
            )

        # User/project configured servers are lazy-started via ClientManager.
        if server_name in configured_servers:
            configured = configured_servers[server_name]
            missing_env_vars = self._missing_remote_header_env_vars(configured)
            if missing_env_vars:
                env_names = ", ".join(missing_env_vars)
                return ProvisionOutput(
                    ok=False,
                    server=server_name,
                    status="failed",
                    message=(
                        f"Server '{server_name}' requires remote header authentication. "
                        f"Set missing environment variable(s): {env_names}"
                    ),
                    missing_env_vars=missing_env_vars,
                    auth_state="missing_auth",
                    auth_methods=self._auth_methods_for_server(server_name),
                    next_step=f"gateway.auth_connect(server_name='{server_name}')",
                    feedback_hint=self._feedback_hint(),
                )
            credential_gap = self._configured_duplicate_missing_credential(
                server_name, configured
            )
            if credential_gap:
                manifest_server, auth_env_options = credential_gap
                return ProvisionOutput(
                    ok=False,
                    server=server_name,
                    status="failed",
                    message=(
                        f"Server '{server_name}' requires authentication. "
                        f"Set one of: {', '.join(auth_env_options)}"
                    ),
                    needs_api_key=True,
                    env_var=manifest_server.env_var,
                    env_instructions=manifest_server.env_instructions,
                    auth_required=True,
                    auth_mode="api_key",
                    auth_methods=self._auth_methods_for_server(server_name),
                    alternative_env_vars=auth_env_options,
                    missing_env_vars=auth_env_options,
                    auth_state="missing_auth",
                    auth_metadata=self._auth_metadata_for_server(manifest_server),
                    next_step=f"gateway.auth_connect(server_name='{server_name}')",
                    feedback_hint=self._feedback_hint(),
                )
            try:
                connected = await self._client_manager.ensure_connected(server_name)
            except ValueError:
                connected = False
            except Exception as e:
                url_elicitations = parse_url_elicitation_error(e)
                if url_elicitations:
                    return ProvisionOutput(
                        ok=False,
                        server=server_name,
                        status="failed",
                        message="URL-mode elicitation required.",
                        auth_required=True,
                        auth_mode="url_elicitation",
                        auth_methods=self._auth_methods_for_server(server_name),
                        auth_state="elicitation_required",
                        next_step=url_elicitations[0].next_step,
                        url_elicitations=url_elicitations,
                        feedback_hint=self._feedback_hint(),
                    )
                raise

            if connected:
                tools = [
                    t
                    for t in self._client_manager.get_all_tools()
                    if t.server_name == server_name
                ]
                return ProvisionOutput(
                    ok=True,
                    server=server_name,
                    status="complete",
                    message=f"Server '{server_name}' started from .mcp.json configuration with {len(tools)} tools.",
                    new_tools=[
                        CapabilityCard(
                            tool_id=t.tool_id,
                            server=t.server_name,
                            tool_name=t.tool_name,
                            short_description=t.short_description,
                            tags=t.tags,
                            availability="online",
                            risk_hint=t.risk_hint.value,
                        )
                        for t in tools[:10]
                    ],
                    feedback_hint=None,
                )

            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=(
                    f"Server '{server_name}' is configured but could not be started. "
                    "Run gateway.refresh and check gateway.health for connection errors."
                ),
                auth_state="unknown",
                feedback_hint=self._feedback_hint(),
            )

        # Load manifest
        manifest = load_manifest()
        server_config = manifest.get_server(server_name)

        if not server_config:
            server_config = self._discovered_server_configs.get(server_name)

        if not server_config:
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=(
                    f"Server '{server_name}' not found in manifest or .mcp.json configuration."
                ),
                feedback_hint=self._feedback_hint(),
            )

        # Check API key if required
        if requires_credential(server_config) and server_config.env_var:
            auth_env_options = self._auth_env_options(
                server_name, server_config.env_var
            )
            if not self._check_any_api_key_available(auth_env_options):
                return ProvisionOutput(
                    ok=False,
                    server=server_name,
                    status="failed",
                    message=(
                        f"Server '{server_name}' requires authentication. "
                        f"Set one of: {', '.join(auth_env_options)}"
                    ),
                    needs_api_key=True,
                    env_var=server_config.env_var,
                    env_instructions=server_config.env_instructions,
                    auth_required=True,
                    auth_mode="api_key",
                    auth_methods=self._auth_methods_for_server(server_name),
                    alternative_env_vars=auth_env_options,
                    missing_env_vars=auth_env_options,
                    auth_state="missing_auth",
                    auth_metadata=self._auth_metadata_for_server(server_config),
                    next_step=f"gateway.auth_connect(server_name='{server_name}')",
                    feedback_hint=self._feedback_hint(),
                )

        # Remote manifest entries do not install packages; connect them directly.
        if server_config.url:
            try:
                resolved_config = manifest_server_to_config(server_config)
                missing_env_vars = self._missing_remote_header_env_vars(resolved_config)
                if missing_env_vars:
                    env_names = ", ".join(missing_env_vars)
                    return ProvisionOutput(
                        ok=False,
                        server=server_name,
                        status="failed",
                        message=(
                            f"Server '{server_name}' requires remote header authentication. "
                            f"Set missing environment variable(s): {env_names}"
                        ),
                        missing_env_vars=missing_env_vars,
                        auth_state="missing_auth",
                        auth_methods=self._auth_methods_for_server(server_name),
                        auth_metadata=self._auth_metadata_for_server(server_config),
                        next_step=f"gateway.auth_connect(server_name='{server_name}')",
                        feedback_hint=self._feedback_hint(),
                    )
                errors = await self._client_manager.connect_all([resolved_config])
                if errors:
                    message = "; ".join(errors)
                    url_elicitations = parse_url_elicitation_error(message)
                    if url_elicitations:
                        self._record_feedback_event(
                            "provision_failure",
                            {
                                "server": server_name,
                                "reason": "url_elicitation_required",
                            },
                        )
                        return ProvisionOutput(
                            ok=False,
                            server=server_name,
                            status="failed",
                            message="URL-mode elicitation required.",
                            auth_required=True,
                            auth_mode="url_elicitation",
                            auth_methods=self._auth_methods_for_server(server_name),
                            auth_state="elicitation_required",
                            auth_metadata=self._auth_metadata_for_server(server_config),
                            next_step=url_elicitations[0].next_step,
                            url_elicitations=url_elicitations,
                            feedback_hint=self._feedback_hint(),
                        )
                    auth_challenge = self._auth_challenge_from_message(message)
                    auth_state = "missing_auth" if auth_challenge else "unknown"
                    if auth_challenge and auth_challenge.missing_scopes:
                        auth_state = "insufficient_scope"
                    self._record_feedback_event(
                        "provision_failure",
                        {
                            "server": server_name,
                            "reason": "remote_connect_error",
                            "error": self._sanitize_error(Exception(message)),
                        },
                    )
                    return ProvisionOutput(
                        ok=False,
                        server=server_name,
                        status="failed",
                        message=self._sanitize_error(Exception(message)),
                        auth_state=cast(Any, auth_state),
                        auth_challenge=auth_challenge,
                        auth_metadata=self._auth_metadata_for_server(server_config),
                        next_step="Resolve remote authorization out of band, then retry gateway.provision.",
                        feedback_hint=self._feedback_hint(),
                    )

                self._register_provisioned_server(
                    server_name,
                    server_config.env_var if server_config else None,
                )
                tools = [
                    t
                    for t in self._client_manager.get_all_tools()
                    if t.server_name == server_name
                ]
                return ProvisionOutput(
                    ok=True,
                    server=server_name,
                    status="complete",
                    message=f"Remote server '{server_name}' connected with {len(tools)} tools.",
                    new_tools=[
                        CapabilityCard(
                            tool_id=t.tool_id,
                            server=t.server_name,
                            tool_name=t.tool_name,
                            short_description=t.short_description,
                            tags=t.tags,
                            availability="online",
                            risk_hint=t.risk_hint.value,
                        )
                        for t in tools[:10]
                    ],
                    feedback_hint=None,
                )

            except MissingRemoteHeaderAuthError as e:
                env_names = ", ".join(e.missing_env_vars)
                return ProvisionOutput(
                    ok=False,
                    server=server_name,
                    status="failed",
                    message=(
                        f"Server '{server_name}' requires remote header authentication. "
                        f"Set missing environment variable(s): {env_names}"
                    ),
                    missing_env_vars=e.missing_env_vars,
                    auth_state="missing_auth",
                    auth_metadata=self._auth_metadata_for_server(server_config),
                    next_step=f"gateway.auth_connect(server_name='{server_name}')",
                    feedback_hint=self._feedback_hint(),
                )

            except Exception as e:
                url_elicitations = parse_url_elicitation_error(e)
                if url_elicitations:
                    return ProvisionOutput(
                        ok=False,
                        server=server_name,
                        status="failed",
                        message="URL-mode elicitation required.",
                        auth_required=True,
                        auth_mode="url_elicitation",
                        auth_methods=self._auth_methods_for_server(server_name),
                        auth_state="elicitation_required",
                        auth_metadata=self._auth_metadata_for_server(server_config),
                        next_step=url_elicitations[0].next_step,
                        url_elicitations=url_elicitations,
                        feedback_hint=self._feedback_hint(),
                    )
                logger.error(f"Failed to connect remote server {server_name}: {e}")
                self._record_feedback_event(
                    "provision_failure",
                    {
                        "server": server_name,
                        "reason": "remote_connect_exception",
                        "error": self._sanitize_error(e),
                    },
                )
                return ProvisionOutput(
                    ok=False,
                    server=server_name,
                    status="failed",
                    message=f"Failed to connect remote server '{server_name}': {self._sanitize_error(e)}",
                    auth_state="unknown",
                    auth_metadata=self._auth_metadata_for_server(server_config),
                    feedback_hint=self._feedback_hint(),
                )

        # Start background installation
        platform = getattr(self, "_platform", None) or detect_platform()
        job_manager = get_job_manager()

        try:
            job_id = await job_manager.start_install(server_config, platform)

            return ProvisionOutput(
                ok=True,
                server=server_name,
                status="started",
                job_id=job_id,
                message=f"Installation started for '{server_name}'. Poll gateway.provision_status('{job_id}') for progress.",
                feedback_hint=None,
            )

        except MissingApiKeyError as e:
            self._record_feedback_event(
                "provision_failure",
                {
                    "server": server_name,
                    "reason": "missing_api_key",
                    "env_var": e.env_var,
                },
            )
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=f"Server '{server_name}' requires API key.",
                needs_api_key=True,
                env_var=e.env_var,
                missing_env_vars=[e.env_var],
                env_instructions=e.env_instructions,
                auth_required=True,
                auth_mode="api_key",
                auth_methods=self._auth_methods_for_server(server_name),
                auth_state="missing_auth",
                next_step=f"gateway.auth_connect(server_name='{server_name}')",
                auth_metadata=self._auth_metadata_for_server(server_config),
                feedback_hint=self._feedback_hint(),
            )

        except InstallError as e:
            self._record_feedback_event(
                "provision_failure",
                {
                    "server": server_name,
                    "reason": "install_error",
                    "error": self._sanitize_error(e),
                },
            )
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=self._sanitize_error(e),
                feedback_hint=self._feedback_hint(),
            )

        except Exception as e:
            logger.error(f"Failed to start provisioning {server_name}: {e}")
            self._record_feedback_event(
                "provision_failure",
                {
                    "server": server_name,
                    "reason": "exception",
                    "error": self._sanitize_error(e),
                },
            )
            return ProvisionOutput(
                ok=False,
                server=server_name,
                status="failed",
                message=f"Failed to start provisioning '{server_name}': {self._sanitize_error(e)}",
                feedback_hint=self._feedback_hint(),
            )

    async def auth_connect(self, input_data: dict[str, Any]) -> AuthConnectOutput:
        """gateway.auth_connect - Store auth credentials for a server."""
        audit_started_at = time.monotonic()
        parsed = AuthConnectInput.model_validate(input_data)
        server_name = parsed.server_name

        if parsed.auth_mode == "url_elicitation":
            if parsed.credential:
                self._audit(
                    method="gateway.auth_connect",
                    action="auth_connect",
                    outcome="refused",
                    started_at=audit_started_at,
                    server_name=server_name,
                    auth_state="elicitation_required",
                    auth_event="url_elicitation_required",
                    error="URL-mode elicitation does not accept credentials.",
                )
                return AuthConnectOutput(
                    ok=False,
                    server=server_name,
                    message=(
                        "URL-mode elicitation does not accept credentials, OAuth "
                        "codes, or third-party secrets through gateway.auth_connect."
                    ),
                    auth_state="elicitation_required",
                )
            if not parsed.elicitation_id or not parsed.consent_acknowledged:
                self._audit(
                    method="gateway.auth_connect",
                    action="auth_connect",
                    outcome="refused",
                    started_at=audit_started_at,
                    server_name=server_name,
                    auth_state="elicitation_required",
                    auth_event="url_elicitation_required",
                    error="URL-mode elicitation acknowledgement is incomplete.",
                )
                return AuthConnectOutput(
                    ok=False,
                    server=server_name,
                    message=(
                        "URL-mode elicitation requires elicitation_id and "
                        "consent_acknowledged=true after completing the URL flow."
                    ),
                    auth_state="elicitation_required",
                    next_step=(
                        "Complete the provider URL flow, then call "
                        "gateway.auth_connect with auth_mode='url_elicitation', "
                        "elicitation_id, and consent_acknowledged=true."
                    ),
                )
            url_elicitation = None
            if parsed.elicitation_url:
                try:
                    # Operator provenance: this URL was typed into
                    # gateway.auth_connect, not relayed from a server. Local
                    # OAuth redirects to http://127.0.0.1, so loopback HTTP
                    # stays allowed here and only here (#211).
                    safe_url = sanitize_url_elicitation_url(
                        parsed.elicitation_url, provenance="operator"
                    )
                except ValueError as e:
                    self._audit(
                        method="gateway.auth_connect",
                        action="auth_connect",
                        outcome="refused",
                        started_at=audit_started_at,
                        server_name=server_name,
                        auth_state="elicitation_required",
                        auth_event="url_elicitation_required",
                        error=str(e),
                    )
                    return AuthConnectOutput(
                        ok=False,
                        server=server_name,
                        message=str(e),
                        auth_state="elicitation_required",
                    )
                retry_step = f"Retry gateway.provision(server_name='{server_name}') or gateway.invoke."
                url_verified = is_verified_public_auth_url(parsed.elicitation_url)
                url_elicitation = UrlElicitationInfo(
                    elicitation_id=parsed.elicitation_id,
                    url=safe_url,
                    url_verified=url_verified,
                    next_step=retry_step
                    if url_verified
                    else f"{UNVERIFIED_URL_CAVEAT} {retry_step}",
                )
            self._audit(
                method="gateway.auth_connect",
                action="auth_connect",
                outcome="success",
                started_at=audit_started_at,
                server_name=server_name,
                auth_state="elicitation_required",
                auth_event="url_elicitation_acknowledged",
            )
            return AuthConnectOutput(
                ok=True,
                server=server_name,
                message=(
                    f"Recorded URL-mode elicitation acknowledgement for '{server_name}'. "
                    "PMCP did not store third-party credentials."
                ),
                next_step=f"Retry gateway.provision(server_name='{server_name}') or gateway.invoke.",
                auth_state="elicitation_required",
                url_elicitation=url_elicitation,
            )

        if not parsed.credential:
            self._audit(
                method="gateway.auth_connect",
                action="auth_connect",
                outcome="refused",
                started_at=audit_started_at,
                server_name=server_name,
                auth_state="missing_auth",
                auth_event="missing_credential",
                error="API-key auth requires a credential value.",
            )
            return AuthConnectOutput(
                ok=False,
                server=server_name,
                message="API-key auth requires a credential value.",
                auth_state="missing_auth",
            )

        manifest = load_manifest()
        server_config = manifest.get_server(server_name)
        # Resolve the server's declared credential variable from both the
        # manifest and the discovered-server registry: discovered servers
        # (register_discovered_server) never land in the manifest, so relying on
        # manifest.get_server alone would break their register→auth→provision
        # flow.
        declared_env_var = server_config.env_var if server_config else None
        declared_storage_key = (
            credential_storage_key(server_config) if server_config else None
        )
        if declared_env_var is None:
            discovered = self._discovered_server_configs.get(server_name)
            if discovered is not None:
                declared_env_var = discovered.env_var
                declared_storage_key = credential_storage_key(discovered)
        # Persist under the (optionally namespaced) storage key so generic runtime
        # names like API_TOKEN do not collide in the flat secret store. An explicit
        # override that names the runtime env_var (following env_instructions like
        # "Set API_TOKEN") is normalized to the storage key so it still lands
        # namespaced; any other override is honored for discovered/advanced flows.
        env_var: str | None
        if (
            parsed.env_var
            and declared_storage_key is not None
            and parsed.env_var
            in {
                declared_env_var,
                declared_storage_key,
            }
        ):
            env_var = declared_storage_key
        else:
            env_var = parsed.env_var or declared_storage_key or declared_env_var

        if not env_var:
            self._audit(
                method="gateway.auth_connect",
                action="auth_connect",
                outcome="refused",
                started_at=audit_started_at,
                server_name=server_name,
                auth_state="missing_auth",
                auth_event="missing_credential",
                error="No auth env var is known.",
            )
            return AuthConnectOutput(
                ok=False,
                server=server_name,
                message=(
                    f"No auth env var is known for server '{server_name}'. "
                    "Pass env_var explicitly or add auth metadata to manifest."
                ),
                auth_state="missing_auth",
            )

        # A caller-chosen key is written into process env and the persistent
        # .env, then read back when the subprocess is provisioned. Only the
        # server's declared storage key (or a credential-shaped name when none is
        # declared) is permitted; loader-influencing variables such as
        # LD_PRELOAD / NODE_OPTIONS / PATH / PYTHON* are refused outright.
        if not env_var_allowed(env_var, declared_storage_key):
            self._audit(
                method="gateway.auth_connect",
                action="auth_connect",
                outcome="refused",
                started_at=audit_started_at,
                server_name=server_name,
                auth_state="missing_auth",
                auth_event="policy_denied",
                error=f"Env var '{env_var}' is not permitted for this server.",
            )
            expected = (
                f" Expected '{declared_storage_key}'." if declared_storage_key else ""
            )
            return AuthConnectOutput(
                ok=False,
                server=server_name,
                message=(
                    f"Env var '{env_var}' is not permitted for server "
                    f"'{server_name}'.{expected} Refusing to store it."
                ),
                auth_state="missing_auth",
                env_var=env_var,
            )

        try:
            path = self._write_secret(parsed.scope, env_var, parsed.credential)
        except ValueError as exc:
            self._audit(
                method="gateway.auth_connect",
                action="auth_connect",
                outcome="refused",
                started_at=audit_started_at,
                server_name=server_name,
                auth_state="missing_auth",
                auth_event="missing_credential",
                error=str(exc),
            )
            return AuthConnectOutput(
                ok=False,
                server=server_name,
                message=str(exc),
                auth_state="missing_auth",
                env_var=env_var,
            )
        os.environ[env_var] = parsed.credential

        self._audit(
            method="gateway.auth_connect",
            action="auth_connect",
            outcome="success",
            started_at=audit_started_at,
            server_name=server_name,
            auth_state="none",
            auth_event="credential_stored",
        )
        return AuthConnectOutput(
            ok=True,
            server=server_name,
            message=(
                f"Stored credential for '{server_name}' in {parsed.scope} scope. "
                "You can now call gateway.provision."
            ),
            env_var=env_var,
            env_path=str(path),
            next_step=f"gateway.provision(server_name='{server_name}')",
        )

    async def submit_feedback(self, input_data: dict[str, Any]) -> SubmitFeedbackOutput:
        """gateway.submit_feedback - Prepare/submit PMCP feedback issue."""
        parsed = SubmitFeedbackInput.model_validate(input_data)
        repository = os.environ.get("PMCP_FEEDBACK_REPO", "ViperJuice/pmcp")

        warning = (
            "Submission sends technical telemetry to GitHub and may consume model tokens. "
            "Send technical data only; never include personal data or secrets."
        )

        if not self._telemetry_enabled():
            return SubmitFeedbackOutput(
                ok=False,
                submitted=False,
                repository=repository,
                repository_visibility="unknown",
                issue_title=parsed.title,
                issue_body=parsed.description,
                warning=warning,
                message="Feedback telemetry is disabled in guidance config (enable_telemetry=false).",
            )

        self._record_feedback_event(
            "feedback_prepare",
            {
                "issue_type": parsed.issue_type,
                "subordinate_server": parsed.subordinate_server,
                "failed_tool_call": parsed.failed_tool_call,
            },
        )

        issue_title, issue_body = self._build_feedback_issue(parsed, repository)

        if not parsed.confirm_submission:
            return SubmitFeedbackOutput(
                ok=True,
                submitted=False,
                repository=repository,
                repository_visibility="public",
                issue_title=issue_title,
                issue_body=issue_body,
                warning=warning,
                message=(
                    "Preview generated. Ask the user for consent using your question tool, "
                    "show this exact issue payload, then call again with confirm_submission=true."
                ),
            )

        token = os.environ.get("PMCP_FEEDBACK_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            try:
                from urllib.request import Request

                repo_visibility = "unknown"
                try:
                    repo_req = Request(
                        f"https://api.github.com/repos/{repository}",
                        headers={
                            "Accept": "application/vnd.github+json",
                            "Authorization": f"Bearer {token}",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        method="GET",
                    )
                    with urlopen(repo_req, timeout=5) as repo_resp:  # nosec B310
                        repo_info = json.loads(repo_resp.read().decode("utf-8"))
                    private_flag = bool(repo_info.get("private"))
                    repo_visibility = "private" if private_flag else "public"
                except Exception:
                    repo_visibility = "unknown"

                issue_api = f"https://api.github.com/repos/{repository}/issues"
                payload = json.dumps(
                    {
                        "title": issue_title,
                        "body": issue_body,
                        "labels": [
                            "pmcp-feedback",
                            "authenticated-feedback",
                            parsed.issue_type,
                        ],
                    }
                ).encode("utf-8")
                req = Request(
                    issue_api,
                    data=payload,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token}",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=10) as resp:  # nosec B310
                    created = json.loads(resp.read().decode("utf-8"))

                issue_url = created.get("html_url")
                issue_number = created.get("number")
                self._record_feedback_event(
                    "feedback_submitted",
                    {
                        "repository": repository,
                        "issue_url": issue_url,
                        "authenticated": True,
                    },
                )
                return SubmitFeedbackOutput(
                    ok=True,
                    submitted=True,
                    repository=repository,
                    repository_visibility=cast(
                        Literal["public", "private", "unknown"], repo_visibility
                    ),
                    issue_title=issue_title,
                    issue_body=issue_body,
                    issue_url=issue_url,
                    issue_number=issue_number,
                    authenticated=True,
                    warning=warning,
                    message=(
                        "Feedback issue submitted successfully. "
                        "Share issue_url with the user so they can review/delete it if desired."
                    ),
                )
            except Exception as e:
                logger.warning(f"Authenticated feedback submission failed: {e}")

        if shutil.which("gh"):
            cmd = [
                "gh",
                "issue",
                "create",
                "--repo",
                repository,
                "--title",
                issue_title,
                "--body",
                issue_body,
                "--label",
                "pmcp-feedback",
                "--label",
                parsed.issue_type,
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                issue_url = stdout.decode("utf-8", errors="replace").strip()
                self._record_feedback_event(
                    "feedback_submitted",
                    {
                        "repository": repository,
                        "issue_url": issue_url,
                        "authenticated": False,
                    },
                )
                return SubmitFeedbackOutput(
                    ok=True,
                    submitted=True,
                    repository=repository,
                    repository_visibility="public",
                    issue_title=issue_title,
                    issue_body=issue_body,
                    issue_url=issue_url,
                    authenticated=False,
                    warning=warning,
                    message=(
                        "Feedback issue submitted via gh CLI. "
                        "Share issue_url with the user so they can review/delete it if desired."
                    ),
                )
            logger.warning(
                "gh issue create failed: "
                + stderr.decode("utf-8", errors="replace").strip()
            )

        browser_url = f"https://github.com/{repository}/issues/new?" + urlencode(
            {"title": issue_title, "body": issue_body}
        )
        return SubmitFeedbackOutput(
            ok=True,
            submitted=False,
            repository=repository,
            repository_visibility="public",
            issue_title=issue_title,
            issue_body=issue_body,
            issue_url=browser_url,
            warning=warning,
            message=(
                "Could not auto-submit. Open issue_url to submit manually. "
                "Share with user so they can edit/remove content before posting."
            ),
        )

    async def update_server(self, input_data: dict[str, Any]) -> UpdateServerOutput:
        """gateway.update_server - Update a subordinate MCP package and restart it.

        The probe below is a 60-second await, and the config is re-resolved
        after it before anything restarts. Two DIFFERENT things happen to the
        child environment across that window, and they must not be described as
        one:

        1. A CONFIG-DRIVEN environment change is REFUSED. This includes every
           manifest credential -- ``_manifest_server_to_config`` resolves it
           into ``LocalMcpServerConfig.env`` at load time, so rotating it during
           the probe makes the recheck's child environment differ from the
           probe's and this tool returns ``ok=False`` without restarting: the
           package is fetched but NOT activated and no version is recorded. The
           guarantee is "the config restarted onto is the config that was
           probed" (Consiliency/pmcp#151), so the operator's rotation applies on
           the next update rather than to a process that was probed with the old
           value.

        2. A GENUINELY AMBIENT variable is LIVE at spawn. Both sides of the
           guard below derive from ONE ``stripped_base``, so an ambient change
           affects them equally and cancels out -- it can never cause a refusal.
           ``sanitized_subprocess_env`` then calls ``os.environ.copy()`` at
           spawn time, the same read ``connect``, ``refresh`` and
           auto-reconnect reach, so the restarted server gets the value in force
           when it is spawned, not the one in force when the update began.

        Freezing the ambient environment across the update is deliberately NOT
        done here; it would mean threading a frozen env through ClientManager,
        which is a separate concern from this TOCTOU.
        """
        parsed = UpdateServerInput.model_validate(input_data)
        server_name = parsed.server_name

        # Resolve the server's EFFECTIVE config through the exact same
        # function -- and therefore the exact same .mcp.json-over-manifest
        # precedence -- gateway.restart_server itself uses below. Board
        # review: this tool previously resolved the probe target through
        # _get_server_config_for_update, which only ever consults the
        # manifest/discovered servers and never .mcp.json. For a server
        # configured in both places (README documents pinning a server's
        # version via .mcp.json as the supported override channel), that
        # meant probing one command while restarting a different one --
        # structurally capable of probing/installing upstream @latest while
        # restarting onto a completely different, e.g. pinned, config.
        # Resolving through restart_server's own resolver makes that
        # divergence impossible for a STABLE configuration: both reads are the
        # same lookup, so identical inputs cannot produce different answers.
        # They are still two separate reads of a file that can change between
        # them, which is why the restart below re-resolves and verifies rather
        # than assuming this one still holds (Consiliency/pmcp#151).
        prior_status = self._status_value(server_name)
        resolved_config, resolve_failure = self._resolve_lifecycle_config(
            server_name, action="restart", prior_status=prior_status
        )
        if resolve_failure is not None:
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type="unknown",
                message=resolve_failure.message,
            )
        if resolved_config is None:
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type="unknown",
                message=f"Server '{server_name}' could not be resolved.",
            )

        if isinstance(resolved_config.config, LocalMcpServerConfig):
            command = resolved_config.config.command
            args = resolved_config.config.args
            # `LocalMcpServerConfig` is the type that carries a `cwd`, and its
            # `env` is this server's own declared OVERLAY -- exactly what the
            # npm identity gate needs. (`sanitized_subprocess_env` merges this
            # overlay onto `os.environ` for the probe below; the merged result
            # must NOT be passed here, since every process has PATH and HOME and
            # the gate would refuse every npm server.)
            server_env: Mapping[str, str] | None = resolved_config.config.env
            server_cwd = resolved_config.config.cwd
        else:
            # Remote servers have no local package for this tool to update.
            command, args = "", []
            server_env, server_cwd = None, None

        package_type, package_name = detect_package_type(
            command, args, server_env, server_cwd
        )
        if package_type == "unknown" or not package_name:
            # Name the actual command line. Without it, a server launched as
            # `npm run mcp` reads "could not determine package manager" and
            # then "Supported managers: npm (npx)" -- which looks like a
            # contradiction, since npm plainly IS supported. The real reason is
            # narrower: that command line names no *registry package* to update
            # (a package.json script is not one), so there is nothing for this
            # tool to fetch (ah board review, correctness seat).
            invocation = " ".join([command, *args]).strip() if command else ""
            detail = f" from `{invocation}`" if invocation else ""
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type="unknown",
                message=(
                    f"Could not determine a registry package to update for "
                    f"'{server_name}'{detail}. Supported managers: npm (npx), "
                    "pypi (uvx/pip), cargo, docker."
                ),
            )

        pinned_to = _detect_effective_version_pin(
            package_type, command, args, server_env, server_cwd
        )
        if pinned_to is not None:
            source_desc = _CONFIG_SOURCE_LABELS.get(
                resolved_config.source, f"the {resolved_config.source} config"
            )
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type=package_type,
                package_name=package_name,
                message=(
                    f"'{server_name}' is pinned to '{pinned_to}' in {source_desc} "
                    f"({command} {' '.join(args)}). gateway.update_server will not "
                    "move a pinned server to the latest version -- edit or remove "
                    "the pin in that config to allow updates."
                ),
            )

        if package_type == "npm":
            update_cmd = ["npx", "-y", f"{package_name}@latest", "--help"]
        elif package_type == "cargo":
            update_cmd = ["cargo", "install", package_name]
        elif package_type == "docker":
            update_cmd = ["docker", "pull", f"{package_name}:latest"]
        else:
            update_cmd = ["uvx", "--refresh", package_name, "--help"]

        try:
            # Sanitized env: the probe executes the server's own package code, so
            # it gets only this server's resolved credential, never other
            # servers'. resolved_config.config.env already carries this
            # server's fully-resolved credential -- for a manifest server,
            # _manifest_server_to_config injects it the same way
            # build_install_child_env used to (same credential_lookup_keys
            # scan); for a .mcp.json server it's whatever that entry declares.
            # Using it here means the probe's env can never diverge from the
            # restart's env either, for the same reason command/args can't.
            probe_env = sanitized_subprocess_env(
                resolved_config.config.env
                if isinstance(resolved_config.config, LocalMcpServerConfig)
                else None,
                self._project_root,
            )
            ok, output = await self._run_update_probe_command(update_cmd, env=probe_env)
        except TimeoutError:
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type=package_type,
                package_name=package_name,
                message="Update probe timed out after 60 seconds.",
            )
        except Exception as e:
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type=package_type,
                package_name=package_name,
                message=f"Failed to run update probe: {e}",
            )

        if not ok:
            short_output = output[-300:] if output else "no output"
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type=package_type,
                package_name=package_name,
                message=f"Update command failed: {short_output}",
            )

        # Consiliency/pmcp#151: the probe above is a 60-second await, and the
        # config loaders re-read from disk on every call, so the configuration
        # backing this update may have changed since it was resolved. Verify
        # BEFORE going any further.
        #
        # This check must precede refresh(). refresh() is itself a diff-based
        # config reconcile: it re-reads the config and disconnects/reconnects
        # servers whose definition changed, which means it can ACTIVATE the
        # freshly-fetched package on its own, via disconnect_server +
        # connect_all rather than restart_server. Checking after it would let
        # this tool report "fetched but not activated" after activation had
        # already happened -- a false statement, and one that a test asserting
        # "restart_server was never called" would not catch (ah board review).
        #
        # The guarantee is "the config restarted onto is the config that was
        # probed" -- NOT "the config on disk when the restart completes". An
        # edit landing after this check applies on the next update.
        recheck_config, recheck_failure = self._resolve_lifecycle_config(
            server_name,
            action="restart",
            prior_status=self._status_value(server_name),
        )
        if recheck_failure is not None or recheck_config is None:
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type=package_type,
                package_name=package_name,
                message=(
                    f"'{server_name}' could no longer be resolved after the update "
                    f"was fetched, so it was not activated: "
                    f"{recheck_failure.message if recheck_failure else 'server not found'}"
                ),
            )
        # Two-part check. _refresh_config_unchanged covers command/args/cwd and
        # is the right predicate for process identity -- it ignores `source`
        # (the same effective server can arrive as manifest or as a configured
        # entry) and compares `env` by effective override only, because naive
        # full-env comparison caused spurious teardowns in #79 and here would
        # cause spurious refusals of legitimate updates.
        #
        # But it is NOT sufficient on its own for this decision (ah board
        # review). It discards an explicit env entry whose value equals the
        # ambient value, while sanitized_subprocess_env strips every
        # PMCP-managed key and restores only explicit entries. So dropping
        # `env={"KEY": <same value as os.environ>}` compares "unchanged" yet
        # silently removes KEY from the spawned process -- and the reverse edit
        # newly exposes a managed credential. That is a material change to what
        # gets launched, which is exactly what this guard exists to catch.
        # Verified empirically, not inferred.
        #
        # This is not a defect in _refresh_config_unchanged for refresh()'s own
        # purpose (deciding whether to tear down a running server); the stakes
        # differ here, where we are deciding whether the thing we probed is the
        # thing we are about to launch.
        #
        # Scope, stated precisely: this detects a CONFIG-driven change to the
        # child environment. It does not freeze the ambient environment across
        # the update -- an os.environ or secret-store change during the probe
        # affects both sides equally and cancels out by construction. Holding
        # the probe's exact environment all the way to the spawn would mean
        # threading a frozen env through ClientManager, which is a separate
        # concern from this TOCTOU (tracked separately).
        #
        # Derive BOTH sides from ONE stripped base. Calling
        # sanitized_subprocess_env twice would re-read the credential files
        # twice (managed_secret_keys hits disk per call), so a write landing
        # between the two reads could make identical configs compare unequal --
        # a spurious refusal on the only update path pmcp has (ah board
        # review, second pass). One read, one base, no window.
        stripped_base = sanitized_subprocess_env(None, self._project_root)

        def _child_env(candidate: ResolvedServerConfig) -> dict[str, str]:
            own = (
                candidate.config.env
                if isinstance(candidate.config, LocalMcpServerConfig)
                else None
            )
            return {**stripped_base, **(own or {})}

        probe_child_env = _child_env(resolved_config)
        restart_child_env = _child_env(recheck_config)
        if (
            not _refresh_config_unchanged(resolved_config, recheck_config)
            or probe_child_env != restart_child_env
        ):
            return UpdateServerOutput(
                ok=False,
                server=server_name,
                package_type=package_type,
                package_name=package_name,
                message=(
                    f"Configuration for '{server_name}' changed while the update "
                    f"was being fetched. The package was fetched but NOT activated, "
                    f"and no version was recorded. Re-run "
                    f"gateway.update_server(server_name='{server_name}') to update "
                    f"against the current configuration."
                ),
            )

        refresh_result = await self.refresh({"reason": f"update_server:{server_name}"})
        latest_version, _ = await get_package_version(
            command, args, server_env, server_cwd, timeout=5.0
        )

        # gateway.refresh() is a diff-based reconcile (_refresh_config_unchanged):
        # it deliberately leaves a server whose command/args are unchanged
        # connected and running, so a version-only update like `npx pkg@latest`
        # never changes argv and refresh() alone never respawns the process --
        # the OLD package keeps serving requests despite the probe having
        # installed the new one. Explicitly restart the target server, reusing
        # the same resolve+disconnect+connect machinery gateway.restart_server
        # already exercises, so the freshly-fetched package is what's actually
        # running.
        restart_result = await self._restart_resolved_server(
            server_name,
            recheck_config,
            force=parsed.force,
            prior_status=self._status_value(server_name),
        )

        # `desc.version` is an upstream snapshot from the last `pmcp
        # refresh`/describe pass, NOT the installed version -- pmcp cannot
        # observe which artifact a running server executes, which is why the
        # automatic update notices were removed (Consiliency/pmcp#150). It is
        # still recorded here so the descriptions cache reflects what this
        # update fetched, and only once the server has actually been restarted
        # onto the new package: a failed or refused restart means the gateway
        # is still serving the old version.
        if restart_result.ok:
            if latest_version:
                if (
                    self._descriptions_cache is not None
                    and server_name in self._descriptions_cache.servers
                ):
                    desc_entry = self._descriptions_cache.servers[server_name]
                    if desc_entry.package != package_name:
                        # A genuine PACKAGE change, not an update of this
                        # entry. Every field describes a different package, so
                        # patching them one by one leaves a permanently MIXED
                        # entry: `capability_summary` is deliberately preserved
                        # on a version bump (see below), but once `package` and
                        # `version` are both rewritten the freshness
                        # short-circuit CONFIRMS identity and never regenerates
                        # the entry -- so the old package's summary would be
                        # served under the new package's label forever, not
                        # merely one refresh cycle behind. Drop it and let the
                        # next refresh rebuild it wholesale: this phase's own
                        # rule applied here -- cannot confirm the cache
                        # describes this package -> refresh, never patch around
                        # it (ah board review, red-team seat).
                        del self._descriptions_cache.servers[server_name]
                    else:
                        desc_entry.version = latest_version
                        # Re-label the entry with the package it now describes, not
                        # just the version. This is a THIRD write site for package
                        # identity alongside refresh_server's constructor and
                        # save_descriptions_cache's dict, and omitting it re-opens
                        # the exact defect the identity gate closes: an entry whose
                        # `package` still names the OLD package while its version
                        # and tools come from the NEW one. `_same_package` would
                        # then CONFIRM identity against the stale label and the
                        # short-circuit would serve the new package's descriptions
                        # under the old package's name. It also strands a legacy
                        # entry as permanently unverifiable, since `package_type`
                        # would stay None even though we have just classified it.
                        # Both are in scope from detect_package_type above, which
                        # already refused an unclassifiable package.
                        desc_entry.package = package_name
                        desc_entry.package_type = package_type
                        # The restart just gave us a live, freshly-connected tool
                        # list for this server -- regenerate `tools`/`generated_at`
                        # from it too, not just `version`. Otherwise the entry
                        # would claim tools were "generated" for a version they
                        # weren't (GeneratedServerDescriptions.version is
                        # documented as "Package version when generated"), and a
                        # package update that changed tool signatures/descriptions
                        # or added/removed tools would keep serving the pre-update
                        # tool list to offline catalog_search. This regenerates
                        # from the connection `restart_server` already made --
                        # no extra stdio round-trip, unlike refresher.refresh_server.
                        # `capability_summary` is deliberately left as-is: it only
                        # feeds GatewayServer.initialize()'s startup MCP-instructions
                        # text (computed once, not read live by catalog_search or
                        # invoke), so leaving it one refresh cycle behind is a much
                        # smaller, non-functional gap than a stale `tools` list was.
                        live_tools = [
                            t
                            for t in self._client_manager.get_all_tools()
                            if t.server_name == server_name
                        ]
                        desc_entry.tools = [
                            PrebuiltToolInfo(
                                name=t.tool_name,
                                description=t.description,
                                short_description=t.short_description,
                                tags=t.tags,
                                risk_hint=t.risk_hint.value,
                            )
                            for t in live_tools
                        ]
                        desc_entry.generated_at = datetime.now(timezone.utc).isoformat()
                    if self._descriptions_cache_path is not None:
                        try:
                            save_descriptions_cache(
                                self._descriptions_cache, self._descriptions_cache_path
                            )
                        except Exception as e:
                            # The package update + restart already succeeded; a
                            # bookkeeping failure here shouldn't flip the
                            # reported result to failed.
                            logger.warning(
                                f"Failed to persist descriptions cache after updating "
                                f"'{server_name}': {e}"
                            )

            message = (
                f"Updated and restarted '{server_name}' ({package_type}:{package_name}); "
                "the new version is now active."
            )
            if not refresh_result.ok:
                message += (
                    " Some other servers failed to reconnect during the gateway "
                    "refresh; inspect gateway.health."
                )
        else:
            restart_errors = (
                "; ".join(restart_result.errors)
                if restart_result.errors
                else restart_result.message
            )
            message = (
                f"Fetched the update for '{server_name}' ({package_type}:{package_name}), "
                f"but could not restart the server to activate it: {restart_errors}. "
                "The gateway is still serving the previous version. Retry "
                f"gateway.update_server(server_name='{server_name}') or "
                f"gateway.restart_server(server_name='{server_name}', force=true)."
            )

        return UpdateServerOutput(
            ok=restart_result.ok,
            server=server_name,
            package_type=package_type,
            package_name=package_name,
            refreshed=refresh_result.ok,
            restarted=restart_result.ok,
            latest_version=latest_version,
            cancelled_request_count=restart_result.cancelled_request_count,
            cancelled_task_count=restart_result.cancelled_task_count,
            message=message,
        )

    async def search_registry(self, input_data: dict[str, Any]) -> SearchRegistryOutput:
        """gateway.search_registry - Search public MCP Registry for external servers."""
        parsed = SearchRegistryInput.model_validate(input_data)
        self._record_feedback_event("registry_search", {"query": parsed.query})

        results: list[SearchRegistryResult] = []
        seen_identities: set[str] = set()
        entries = await self._registry_matches(parsed.query, limit=parsed.limit * 2)
        for entry in entries:
            package = entry.packages[0] if entry.packages else None
            remote = entry.remotes[0] if entry.remotes else None
            identity = (
                package.identifier
                if package is not None
                else remote.url
                if remote is not None and remote.url is not None
                else entry.name
            )
            if not identity or identity in seen_identities:
                continue
            seen_identities.add(identity)
            env_vars = package.env_vars if package is not None else []
            remote_headers = remote.headers if remote is not None else []
            results.append(
                SearchRegistryResult(
                    name=entry.name,
                    package=identity,
                    description=entry.description,
                    transport=(
                        remote.transport
                        if remote is not None
                        else package.transport
                        if package is not None
                        else None
                    ),
                    env_vars=env_vars,
                    url=(
                        remote.url
                        if remote is not None
                        else package.url
                        if package is not None
                        else None
                    ),
                    remotes=[remote.raw for remote in entry.remotes],
                    remote_headers=remote_headers,
                    registry_status=entry.registry_meta.status,
                    is_latest=entry.registry_meta.is_latest,
                    server_card_url=entry.server_card_url,
                    protected_resource_metadata_url=entry.protected_resource_metadata_url,
                    authorization_server_metadata_url=entry.authorization_server_metadata_url,
                    declared_scopes=entry.declared_scopes,
                    declared_capabilities=entry.declared_capabilities,
                    diagnostics=entry.diagnostics,
                )
            )
            if len(results) >= parsed.limit:
                break

        return SearchRegistryOutput(
            query=parsed.query,
            results=results,
            next_step=(
                "Call gateway.register_discovered_server(package=<package>, server_name=<name>) "
                "then gateway.provision(server_name=<name>) to install."
            ),
        )

    async def register_discovered_server(
        self, input_data: dict[str, Any]
    ) -> RegisterDiscoveredServerOutput:
        """gateway.register_discovered_server - Register an external server for provisioning."""
        parsed = RegisterDiscoveredServerInput.model_validate(input_data)
        server_name = parsed.server_name
        package = parsed.package

        # Defense in depth: the pydantic field validator already rejects unsafe
        # identifiers, but re-check here before the package is baked into an
        # install command that gateway.provision will exec as list-argv.
        if not is_valid_package_name(package):
            return RegisterDiscoveredServerOutput(
                ok=False,
                server_name=server_name,
                registered=False,
                message=(
                    f"Refused to register '{server_name}': "
                    f"unsafe package identifier {package!r}."
                ),
            )

        install_command = ["npx", "-y", package]

        requires_api_key = len(parsed.env_vars) > 0
        # Primary env var used for availability checks and auth_connect prompt
        env_var = parsed.env_vars[0] if parsed.env_vars else None
        # Build instructions for any additional required env vars
        extra_vars = parsed.env_vars[1:] if len(parsed.env_vars) > 1 else []
        env_instructions: str | None = None
        if extra_vars:
            vars_list = ", ".join(f"${v}" for v in extra_vars)
            env_instructions = (
                f"Also set {vars_list} — call gateway.auth_connect for each, "
                "or export them before calling gateway.provision."
            )

        self._discovered_server_configs[server_name] = ServerConfig(
            name=server_name,
            description=parsed.description or f"Discovered MCP package: {package}",
            keywords=["mcp", "discovered", server_name, package],
            install={
                "mac": list(install_command),
                "linux": list(install_command),
                "wsl": list(install_command),
                "windows": list(install_command),
            },
            command="npx",
            args=["-y", package],
            requires_api_key=requires_api_key,
            env_var=env_var,
            env_instructions=env_instructions,
            package=package,
            declared_capabilities=["discovered"],
            discovery_diagnostics=[
                "registered_discovery_metadata_is_read_only_until_provisioned"
            ],
        )

        self._record_feedback_event(
            "server_registered",
            {"server_name": server_name, "package": package},
        )

        return RegisterDiscoveredServerOutput(
            ok=True,
            server_name=server_name,
            registered=True,
            message=(
                f"Registered '{server_name}' (package: {package}). "
                f"gateway.provision will run: {' '.join(install_command)}. "
                "Call gateway.provision to install and start it."
            ),
            install_command=install_command,
            next_step=f"gateway.provision(server_name='{server_name}')",
        )

    async def provision_status(self, input_data: dict[str, Any]) -> ProvisionJobStatus:
        """gateway.provision_status - Check status of a running installation."""
        import time

        try:
            parsed = ProvisionStatusInput.model_validate(input_data)
            job_id = parsed.job_id

            job_manager = get_job_manager()
            job = job_manager.get_job(job_id)

            if not job:
                return ProvisionJobStatus(
                    job_id=job_id,
                    server="unknown",
                    status="not_found",
                    progress=0,
                    message=f"Job '{job_id}' not found. It may have expired.",
                )

            # Copy job state to avoid race conditions with monitor task
            job_status = job.status
            job_progress = job.progress
            job_error = job.error
            job_server_name = job.server_name
            job_output_lines = list(job.output_lines)  # Copy the list
            elapsed = time.time() - job.started_at

            logger.debug(
                f"provision_status: job={job_id} status={job_status} progress={job_progress}"
            )

            # server_ready → adopt the process; complete (non-npx) → refresh.
            # Both are one-shot per job: serialize on a per-job lock and re-read
            # the live status inside it so two concurrent polls cannot
            # double-adopt or re-refresh a job another poll already finalized.
            if job_status in ("server_ready", "complete"):
                finalize_lock = self._provision_finalize_locks.setdefault(
                    job_id, asyncio.Lock()
                )
                async with finalize_lock:
                    if job_id in self._provision_finalized:
                        return self._build_finalized_status(job, job_id, elapsed)
                    live_status = job.status
                    if live_status == "server_ready":
                        return await self._finalize_server_ready(job, job_id, elapsed)
                    if live_status == "complete":
                        return await self._finalize_complete(job, job_id, elapsed)
                    # Live status changed under us (e.g. failed); fall through
                    # and report it below using refreshed snapshots.
                    job_status = live_status
                    job_progress = job.progress
                    job_error = job.error

            # For other statuses, return current state
            status_messages = {
                "pending": f"Preparing to install '{job_server_name}'...",
                "installing": f"Installing '{job_server_name}'... ({job_progress}%)",
                "server_ready": f"Server '{job_server_name}' starting, connecting...",
                "failed": f"Installation failed: {job_error}",
                "timeout": f"Installation timed out: {job_error}",
            }

            return ProvisionJobStatus(
                job_id=job_id,
                server=job_server_name,
                status=job_status,
                progress=job_progress,
                message=status_messages.get(job_status, f"Status: {job_status}"),
                output_tail=job_output_lines[-5:],
                elapsed_seconds=elapsed,
                error=job_error,
            )

        except Exception as e:
            logger.error(f"provision_status handler failed: {e}", exc_info=True)
            # Return a safe error response instead of crashing
            return ProvisionJobStatus(
                job_id=input_data.get("job_id", "unknown"),
                server="unknown",
                status="failed",
                progress=0,
                message=f"Error checking status: {self._sanitize_error(e)}",
                error=self._sanitize_error(e),
            )

    async def _finalize_server_ready(
        self, job: Any, job_id: str, elapsed: float
    ) -> ProvisionJobStatus:
        """Adopt a server_ready job's process into ClientManager exactly once.

        The caller must hold the job's finalize lock and have confirmed the job
        is not already finalized.
        """
        job_server_name = job.server_name
        job_progress = job.progress
        job_output_lines = list(job.output_lines)

        process = job.process
        if not process or process.returncode is not None:
            # Process died before handoff
            job.status = "failed"
            job.error = "Server process exited before handoff"
            return ProvisionJobStatus(
                job_id=job_id,
                server=job_server_name,
                status="failed",
                progress=job_progress,
                message=f"Server process for '{job_server_name}' exited unexpectedly",
                output_tail=job_output_lines[-5:],
                elapsed_seconds=elapsed,
                error="Process exited before handoff",
            )

        try:
            # Build config from manifest
            manifest = load_manifest()
            server_config = manifest.get_server(job_server_name)
            if not server_config:
                raise ValueError(f"Server '{job_server_name}' not found in manifest")

            resolved_config = manifest_server_to_config(server_config)

            # Adopt the process into ClientManager
            await self._client_manager.adopt_process(
                job_server_name, process, resolved_config
            )

            # Mark job complete and clear process reference
            job.status = "complete"
            job.process = None
            self._provision_finalized.add(job_id)

            # Persist to provisioned registry so refresh() can reconnect it
            env_var = server_config.env_var if server_config else None
            self._register_provisioned_server(job_server_name, env_var)

            # Get the new tools
            tools = [
                t
                for t in self._client_manager.get_all_tools()
                if t.server_name == job_server_name
            ]

            return ProvisionJobStatus(
                job_id=job_id,
                server=job_server_name,
                status="complete",
                progress=100,
                message=f"Server '{job_server_name}' installed and connected with {len(tools)} tools.",
                output_tail=job_output_lines[-5:],
                elapsed_seconds=elapsed,
                new_tools=[
                    CapabilityCard(
                        tool_id=t.tool_id,
                        server=t.server_name,
                        tool_name=t.tool_name,
                        short_description=t.short_description,
                        tags=t.tags,
                        availability="online",
                        risk_hint=t.risk_hint.value,
                    )
                    for t in tools[:10]
                ]
                if tools
                else None,
            )

        except Exception as e:
            logger.error(f"Handoff failed for {job_server_name}: {e}", exc_info=True)
            job.status = "failed"
            job.error = f"Handoff failed: {e}"
            # Kill the orphaned process
            if process and process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            job.process = None

            return ProvisionJobStatus(
                job_id=job_id,
                server=job_server_name,
                status="failed",
                progress=job_progress,
                message=f"Failed to connect to '{job_server_name}': {self._sanitize_error(e)}",
                output_tail=job_output_lines[-5:],
                elapsed_seconds=elapsed,
                error=self._sanitize_error(e),
            )

    async def _finalize_complete(
        self, job: Any, job_id: str, elapsed: float
    ) -> ProvisionJobStatus:
        """Refresh once for a completed (non-npx) install job.

        The caller must hold the job's finalize lock and have confirmed the job
        is not already finalized. Marks the job finalized before refreshing so a
        racing poll cannot trigger a second full refresh.
        """
        job_server_name = job.server_name
        job_output_lines = list(job.output_lines)
        self._provision_finalized.add(job_id)

        tools = []
        refresh_error = None
        try:
            await self.refresh({"reason": f"Provisioned {job_server_name}"})
            # Get the new tools
            tools = [
                t
                for t in self._client_manager.get_all_tools()
                if t.server_name == job_server_name
            ]
        except Exception as e:
            logger.error(f"Failed to refresh after install: {e}")
            refresh_error = self._sanitize_error(e)

        message = f"Server '{job_server_name}' installed"
        if tools:
            message += f" and connected with {len(tools)} tools."
        elif refresh_error:
            message += f" but refresh failed: {refresh_error}"
        else:
            message += " but no tools found. Try gateway.refresh manually."

        return ProvisionJobStatus(
            job_id=job_id,
            server=job_server_name,
            status="complete",
            progress=100,
            message=message,
            output_tail=job_output_lines[-5:],
            elapsed_seconds=elapsed,
            new_tools=[
                CapabilityCard(
                    tool_id=t.tool_id,
                    server=t.server_name,
                    tool_name=t.tool_name,
                    short_description=t.short_description,
                    tags=t.tags,
                    availability="online",
                    risk_hint=t.risk_hint.value,
                )
                for t in tools[:10]
            ]
            if tools
            else None,
            error=refresh_error,
        )

    def _build_finalized_status(
        self, job: Any, job_id: str, elapsed: float
    ) -> ProvisionJobStatus:
        """Read-only terminal status for a job already finalized by another poll.

        Reports the connected tools without re-adopting or re-refreshing.
        """
        job_server_name = job.server_name
        job_output_lines = list(job.output_lines)
        tools = [
            t
            for t in self._client_manager.get_all_tools()
            if t.server_name == job_server_name
        ]
        return ProvisionJobStatus(
            job_id=job_id,
            server=job_server_name,
            status="complete",
            progress=100,
            message=f"Server '{job_server_name}' installed and connected with {len(tools)} tools.",
            output_tail=job_output_lines[-5:],
            elapsed_seconds=elapsed,
            new_tools=[
                CapabilityCard(
                    tool_id=t.tool_id,
                    server=t.server_name,
                    tool_name=t.tool_name,
                    short_description=t.short_description,
                    tags=t.tags,
                    availability="online",
                    risk_hint=t.risk_hint.value,
                )
                for t in tools[:10]
            ]
            if tools
            else None,
        )

    async def sync_environment(
        self, input_data: dict[str, Any]
    ) -> SyncEnvironmentOutput:
        """gateway.sync_environment - Sync environment info from host."""
        parsed = SyncEnvironmentInput.model_validate(input_data)

        # Use provided or detect platform
        if parsed.platform:
            platform = parsed.platform
        else:
            platform = detect_platform()

        manifest = load_manifest()

        # Use provided or probe CLIs
        if parsed.detected_clis:
            detected_clis = set(parsed.detected_clis)
            detected_cli_infos = {
                name: info
                for name, info in self._detected_cli_infos.items()
                if name in detected_clis
            }
        else:
            detected_cli_infos = await probe_clis(
                self._build_cli_probe_configs(manifest)
            )
            detected_clis = set(detected_cli_infos.keys())

        # Store for future use
        self._platform = platform
        self._detected_clis = detected_clis
        self._detected_cli_infos = detected_cli_infos

        return SyncEnvironmentOutput(
            platform=platform,
            detected_clis=list(detected_clis),
            message=f"Environment synced: {platform} with {len(detected_clis)} CLIs detected.",
        )

    async def list_pending(self, input_data: dict[str, Any]) -> ListPendingOutput:
        """gateway.list_pending - List pending tool invocations with health status."""
        import time
        from datetime import datetime, timezone

        parsed = ListPendingInput.model_validate(input_data)

        pending_requests = self._client_manager.get_pending_requests(parsed.server)
        now = time.time()

        requests: list[PendingRequestInfo] = []
        for req in pending_requests:
            state = self._client_manager.get_request_state(req)
            requests.append(
                PendingRequestInfo(
                    request_id=f"{req.server_name}::{req.request_id}",
                    server_name=req.server_name,
                    tool_id=req.tool_id,
                    started_at_iso=datetime.fromtimestamp(
                        req.started_at, tz=timezone.utc
                    ).isoformat(),
                    elapsed_seconds=now - req.started_at,
                    timeout_ms=req.timeout_ms,
                    state=state.value,
                    last_heartbeat_seconds_ago=now - req.last_heartbeat,
                    task_id=getattr(req, "task_id", None),
                    task_status=getattr(req, "task_status", None),
                )
            )

        return ListPendingOutput(
            requests=requests,
            total_pending=len(requests),
        )

    async def tasks_list(self, input_data: dict[str, Any]) -> TasksListOutput:
        """gateway.tasks_list - List downstream MCP tasks."""
        audit_started_at = time.monotonic()
        parsed = TasksListInput.model_validate(input_data)
        if parsed.server_name and not self._policy_manager.is_server_allowed(
            parsed.server_name
        ):
            message = f"Server '{parsed.server_name}' is blocked by policy."
            self._audit(
                method="gateway.tasks_list",
                action="tasks_list",
                outcome="refused",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                auth_state="policy_denied",
                error=message,
            )
            return TasksListOutput(ok=False, errors=[message])
        try:
            server_names: list[str | None]
            if parsed.server_name:
                server_names = [parsed.server_name]
            else:
                discovered_servers = {
                    status.name
                    for status in self._client_manager.get_all_server_statuses()
                }
                discovered_servers.update(
                    tool.server_name for tool in self._client_manager.get_all_tools()
                )
                server_names = [
                    server_name
                    for server_name in sorted(discovered_servers)
                    if self._policy_manager.is_server_allowed(server_name)
                ]

            tasks: list[McpTaskInfo] = []
            next_cursor = None
            for server_name in server_names:
                result = await self._client_manager.list_tasks(
                    server_name,
                    parsed.cursor,
                    requestor_context=parsed.requestor_context,
                )
                next_cursor = result.get("nextCursor") or next_cursor
                for task in result.get("tasks", []):
                    if not isinstance(task, dict) or not isinstance(
                        task.get("task_id"), str
                    ):
                        continue
                    task_server = task.get("server_name", server_name or "")
                    if not isinstance(task_server, str):
                        continue
                    if not self._policy_manager.is_server_allowed(task_server):
                        continue
                    record = self._client_manager.get_task_record(
                        task_server,
                        task["task_id"],
                    )
                    task_info = record if record is not None else McpTaskInfo(**task)
                    tasks.append(self._sanitize_task_for_output(task_info))
            output = TasksListOutput(
                ok=True,
                tasks=tasks,
                next_cursor=next_cursor,
            )
            self._audit(
                method="gateway.tasks_list",
                action="tasks_list",
                outcome="success",
                started_at=audit_started_at,
                server_name=parsed.server_name,
            )
            return output
        except Exception as e:
            self._audit(
                method="gateway.tasks_list",
                action="tasks_list",
                outcome="failure",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                error=str(e),
            )
            return TasksListOutput(ok=False, errors=[self._sanitize_error(e)])

    async def tasks_get(self, input_data: dict[str, Any]) -> TasksGetOutput:
        """gateway.tasks_get - Get a downstream MCP task."""
        audit_started_at = time.monotonic()
        parsed = TasksGetInput.model_validate(input_data)
        if not self._policy_manager.is_server_allowed(parsed.server_name):
            message = f"Server '{parsed.server_name}' is blocked by policy."
            self._audit(
                method="gateway.tasks_get",
                action="tasks_get",
                outcome="refused",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
                auth_state="policy_denied",
                error=message,
            )
            return TasksGetOutput(ok=False, errors=[message])
        try:
            task = await self._client_manager.get_task(
                parsed.server_name,
                parsed.task_id,
                requestor_context=parsed.requestor_context,
            )
            self._audit(
                method="gateway.tasks_get",
                action="tasks_get",
                outcome="success",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
            )
            return TasksGetOutput(ok=True, task=self._sanitize_task_for_output(task))
        except Exception as e:
            self._audit(
                method="gateway.tasks_get",
                action="tasks_get",
                outcome="failure",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
                error=str(e),
            )
            return TasksGetOutput(ok=False, errors=[self._sanitize_error(e)])

    async def tasks_result(self, input_data: dict[str, Any]) -> TasksResultOutput:
        """gateway.tasks_result - Fetch a downstream MCP task result."""
        audit_started_at = time.monotonic()
        parsed = TasksResultInput.model_validate(input_data)
        if not self._policy_manager.is_server_allowed(parsed.server_name):
            message = f"Server '{parsed.server_name}' is blocked by policy."
            self._audit(
                method="gateway.tasks_result",
                action="tasks_result",
                outcome="refused",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
                auth_state="policy_denied",
                error=message,
            )
            return TasksResultOutput(ok=False, errors=[message])
        try:
            result = await self._client_manager.get_task_result(
                parsed.server_name,
                parsed.task_id,
                requestor_context=parsed.requestor_context,
            )
            task = self._client_manager.get_task_record(
                parsed.server_name, parsed.task_id
            )
            result_payload = (
                result.get("result", result) if isinstance(result, dict) else result
            )
            if isinstance(result_payload, str):
                for _ in range(2):
                    try:
                        decoded = json.loads(result_payload)
                    except json.JSONDecodeError:
                        break
                    result_payload = decoded
                    if not isinstance(result_payload, str):
                        break
            max_bytes = None
            if parsed.options and parsed.options.max_output_chars:
                max_bytes = parsed.options.max_output_chars * 4
            redact = parsed.options.redact_secrets if parsed.options else True
            processed = self._policy_manager.process_output(
                result_payload, redact=redact, max_bytes=max_bytes
            )
            output = TasksResultOutput(
                ok=True,
                task=self._sanitize_task_for_output(task) if task is not None else None,
                result=processed["result"],
                truncated=processed["truncated"],
                summary=processed["summary"],
                raw_size_estimate=processed["raw_size"],
            )
            self._audit(
                method="gateway.tasks_result",
                action="tasks_result",
                outcome="success",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
            )
            return output
        except Exception as e:
            self._audit(
                method="gateway.tasks_result",
                action="tasks_result",
                outcome="failure",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
                error=str(e),
            )
            return TasksResultOutput(ok=False, errors=[self._sanitize_error(e)])

    def _sanitize_task_for_output(self, task: McpTaskInfo) -> McpTaskInfo:
        task_data = task.model_dump(mode="json")
        if isinstance(task_data.get("status_message"), str):
            task_data["status_message"] = self._policy_manager.redact_secrets(
                task_data["status_message"]
            )
        task_data["raw"] = self._sanitize_task_raw(task_data.get("raw", {}))
        return McpTaskInfo.model_validate(task_data)

    def _sanitize_task_raw(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._policy_manager.redact_secrets(value)
        if isinstance(value, dict):
            return {key: self._sanitize_task_raw(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_task_raw(item) for item in value]
        return value

    async def tasks_cancel(self, input_data: dict[str, Any]) -> TasksCancelOutput:
        """gateway.tasks_cancel - Cancel a downstream MCP task."""
        audit_started_at = time.monotonic()
        parsed = TasksCancelInput.model_validate(input_data)
        if not self._policy_manager.is_server_allowed(parsed.server_name):
            message = f"Server '{parsed.server_name}' is blocked by policy."
            self._audit(
                method="gateway.tasks_cancel",
                action="tasks_cancel",
                outcome="refused",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
                auth_state="policy_denied",
                error=message,
            )
            return TasksCancelOutput(
                ok=False,
                status="policy_denied",
                message=message,
                errors=[message],
            )
        try:
            ok, task, message = await self._client_manager.cancel_task(
                parsed.server_name,
                parsed.task_id,
                parsed.force,
                requestor_context=parsed.requestor_context,
            )
            self._audit(
                method="gateway.tasks_cancel",
                action="tasks_cancel",
                outcome="success" if ok else "refused",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
                error=None if ok else message,
            )
            return TasksCancelOutput(
                ok=ok,
                task=task,
                status="cancelled" if ok else "not_found",
                message=message,
                errors=None if ok else [message],
            )
        except Exception as e:
            message = self._sanitize_error(e)
            self._audit(
                method="gateway.tasks_cancel",
                action="tasks_cancel",
                outcome="failure",
                started_at=audit_started_at,
                server_name=parsed.server_name,
                task_id=parsed.task_id,
                error=message,
            )
            return TasksCancelOutput(
                ok=False,
                status="error",
                message=message,
                errors=[message],
            )

    async def cancel(self, input_data: dict[str, Any]) -> CancelOutput:
        """gateway.cancel - Cancel a pending tool invocation."""
        audit_started_at = time.monotonic()
        parsed = CancelInput.model_validate(input_data)

        (
            status,
            message,
            was_stalled,
            elapsed,
        ) = await self._client_manager.cancel_request(parsed.request_id, parsed.force)

        outcome: Literal["success", "failure", "refused"] = "success"
        if status in {"cancelled", "already_complete"}:
            outcome = "success"
        elif status == "refused":
            outcome = "refused"
        else:
            outcome = "failure"
        server_name = (
            parsed.request_id.rsplit("::", 1)[0] if "::" in parsed.request_id else None
        )
        self._audit(
            method="gateway.cancel",
            action="cancel",
            outcome=outcome,
            started_at=audit_started_at,
            server_name=server_name,
            error=None if outcome == "success" else message,
        )

        return CancelOutput(
            request_id=parsed.request_id,
            status=status,
            message=message,
            was_stalled=was_stalled,
            elapsed_seconds=elapsed,
        )

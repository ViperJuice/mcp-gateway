"""Capability matcher - match requests to manifest entries using BAML/Groq."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pmcp.manifest.loader import CLIAlternative, Manifest, ServerConfig

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result of capability matching."""

    matched: bool
    entry_name: str
    entry_type: Literal["cli", "server", ""]
    confidence: float
    reasoning: str

    # Resolved config (if matched)
    cli_config: CLIAlternative | None = None
    server_config: ServerConfig | None = None


def _keyword_match_score(query: str, keywords: list[str]) -> float:
    """Simple keyword matching fallback."""
    query_lower = query.lower()
    query_words = set(query_lower.split())

    matches = 0
    for keyword in keywords:
        keyword_lower = keyword.lower()
        if keyword_lower in query_lower or keyword_lower in query_words:
            matches += 1

    if not keywords:
        return 0.0

    return min(matches / len(keywords), 1.0)


async def match_capability(
    query: str,
    manifest: Manifest,
    detected_clis: set[str] | None = None,
    use_llm: bool = True,
) -> MatchResult:
    """Match a capability request to a CLI or MCP server.

    Args:
        query: Natural language capability request
        manifest: Loaded manifest with CLIs and servers
        detected_clis: Set of CLI names detected in the environment
        use_llm: Whether to use LLM for semantic matching (falls back to keyword)

    Returns:
        MatchResult with matched entry or no match
    """
    detected_clis = detected_clis or set()

    # Try LLM-powered matching first
    if use_llm:
        try:
            result = await _llm_match(query, manifest, detected_clis)
            if result.matched:
                return result
        except Exception as e:
            logger.warning(f"LLM matching failed, falling back to keyword: {e}")

    # Fall back to keyword matching
    return _keyword_match(query, manifest, detected_clis)


async def _llm_match(
    query: str,
    manifest: Manifest,
    detected_clis: set[str],
    running_servers: list[str] | None = None,
) -> MatchResult:
    """Use BAML/Groq for semantic matching."""
    from pmcp.baml_client import b
    from pmcp.baml_client.types import (
        ManifestCLI,
        ManifestServer,
        ManifestSummary,
    )

    running_servers = running_servers or []

    # Build ManifestSummary for the LLM
    servers: list[ManifestServer] = []
    clis: list[ManifestCLI] = []

    # Add all servers
    for name, server in manifest.servers.items():
        servers.append(
            ManifestServer(
                name=name,
                description=server.description,
                keywords=server.keywords,
                requires_api_key=server.requires_api_key,
                env_var=server.env_var,
            )
        )

    # Add all CLIs
    for name, cli in manifest.cli_alternatives.items():
        clis.append(
            ManifestCLI(
                name=name,
                description=cli.description,
                keywords=cli.keywords,
            )
        )

    manifest_summary = ManifestSummary(servers=servers, clis=clis)

    # Call BAML function with new API
    result = await b.MatchCapability(
        query=query,
        manifest=manifest_summary,
        available_clis=list(detected_clis),
        running_servers=running_servers,
    )

    # Check if we have any viable candidates
    if not result.candidates:
        return MatchResult(
            matched=False,
            entry_name="",
            entry_type="",
            confidence=0.0,
            reasoning=result.recommendation or "No matching capability found",
        )

    # Get the best candidate (first one with high enough relevance)
    best_candidate = result.candidates[0]

    # Threshold for accepting a match
    if best_candidate.relevance_score < 0.3:
        return MatchResult(
            matched=False,
            entry_name="",
            entry_type="",
            confidence=best_candidate.relevance_score,
            reasoning=f"Best match '{best_candidate.name}' has low relevance: {best_candidate.reasoning}",
        )

    # Resolve the matched entry
    cli_config = None
    server_config = None
    entry_type: Literal["cli", "server", ""] = ""

    if best_candidate.candidate_type == "cli":
        cli_config = manifest.get_cli(best_candidate.name)
        entry_type = "cli"
    elif best_candidate.candidate_type == "server":
        server_config = manifest.get_server(best_candidate.name)
        entry_type = "server"

    return MatchResult(
        matched=True,
        entry_name=best_candidate.name,
        entry_type=entry_type,
        confidence=best_candidate.relevance_score,
        reasoning=best_candidate.reasoning,
        cli_config=cli_config,
        server_config=server_config,
    )


def _keyword_match(
    query: str,
    manifest: Manifest,
    detected_clis: set[str],
) -> MatchResult:
    """Fallback keyword-based matching."""
    best_match: MatchResult | None = None
    best_score = 0.0

    # Check detected CLIs first (preferred)
    for name, cli in manifest.cli_alternatives.items():
        if name in detected_clis:
            score = _keyword_match_score(query, cli.keywords)
            if score > best_score:
                best_score = score
                best_match = MatchResult(
                    matched=True,
                    entry_name=name,
                    entry_type="cli",
                    confidence=score,
                    reasoning=f"Keyword match for installed CLI: {name}",
                    cli_config=cli,
                )

    # Check servers
    for name, server in manifest.servers.items():
        score = _keyword_match_score(query, server.keywords)
        # Slight preference for CLIs, so server needs higher score
        adjusted_score = score * 0.9
        if adjusted_score > best_score:
            best_score = adjusted_score
            best_match = MatchResult(
                matched=True,
                entry_name=name,
                entry_type="server",
                confidence=score,
                reasoning=f"Keyword match for server: {name}",
                server_config=server,
            )

    if best_match and best_score >= 0.2:  # Minimum threshold
        return best_match

    return MatchResult(
        matched=False,
        entry_name="",
        entry_type="",
        confidence=0.0,
        reasoning="No matching capability found in manifest",
    )

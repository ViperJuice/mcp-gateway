"""Manifest module for dynamic capability discovery and provisioning."""

from mcp_gateway.manifest.loader import load_manifest, Manifest
from mcp_gateway.manifest.environment import (
    detect_platform,
    probe_clis,
    EnvironmentInfo,
)
from mcp_gateway.manifest.matcher import match_capability
from mcp_gateway.manifest.installer import install_server

__all__ = [
    "load_manifest",
    "Manifest",
    "detect_platform",
    "probe_clis",
    "EnvironmentInfo",
    "match_capability",
    "install_server",
]

"""Google Cloud managed MCP server integration for ADK agents.

Google Cloud exposes Cloud Run, Cloud Logging, and Compute Engine as managed
MCP servers at https://{service}.googleapis.com/mcp. Auth is via ADC Bearer
tokens injected through McpToolset's header_provider callback.

These supplement (not replace) the native SDK tools in gcp_tools.py — the MCP
servers offer different capabilities and may work when SDK tools hit 403 errors
due to SA permission differences.

Usage:
    from .tools.gcp_mcp import (
        create_cloud_run_toolset,
        create_cloud_logging_toolset,
        create_compute_engine_toolset,
    )

    cloud_run_mcp = create_cloud_run_toolset()
    agent = LlmAgent(tools=build_tool_list(cloud_run_mcp, ...))
"""

import os
from typing import Any

import google.auth
import google.auth.transport.requests
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

GCP_MCP_ENABLED = os.environ.get("GCP_MCP_ENABLED", "true").lower() == "true"

_CLOUD_PLATFORM_SCOPE = ["https://www.googleapis.com/auth/cloud-platform"]

COMPUTE_ENGINE_TOOL_FILTER = [
    "list_instances",
    "get_instance_basic_info",
    "list_instance_attached_disks",
    "get_zone_operation",
    "get_disk_basic_info",
    "list_managed_instances",
    "get_instance_group_manager_basic_info",
    "list_instance_group_managers",
]


def _gcp_auth_headers(_ctx: Any) -> dict[str, str]:
    """header_provider callback — returns a fresh ADC Bearer token + quota project.

    The GCP managed MCP endpoints 403 without an explicit quota/billing project
    header, even when the calling SA has the API roles. X-Goog-User-Project
    pins the consumer project for quota and authorization.
    """
    creds, default_project = google.auth.default(scopes=_CLOUD_PLATFORM_SCOPE)
    creds.refresh(google.auth.transport.requests.Request())
    quota_project = os.environ.get("GOOGLE_CLOUD_PROJECT") or default_project
    return {
        "Authorization": f"Bearer {creds.token}",
        "X-Goog-User-Project": quota_project,
    }


def _create_toolset(
    url: str,
    tool_filter: list[str] | None = None,
) -> McpToolset:
    """Create an McpToolset for a GCP managed MCP server."""
    params = StreamableHTTPConnectionParams(url=url)
    kwargs: dict[str, Any] = {
        "connection_params": params,
        "header_provider": _gcp_auth_headers,
    }
    if tool_filter:
        kwargs["tool_filter"] = tool_filter
    return McpToolset(**kwargs)


# ── Singletons ──────────────────────────────────────────────────────────────

_cloud_run: McpToolset | None = None
_cloud_run_checked = False

_cloud_logging: McpToolset | None = None
_cloud_logging_checked = False

_compute_engine: McpToolset | None = None
_compute_engine_checked = False


def create_cloud_run_toolset() -> McpToolset | None:
    """Cloud Run MCP — 5 tools: get_service, list_services, deploy_service_*."""
    global _cloud_run, _cloud_run_checked
    if _cloud_run_checked:
        return _cloud_run
    _cloud_run_checked = True
    if not GCP_MCP_ENABLED:
        return None
    _cloud_run = _create_toolset("https://run.googleapis.com/mcp")
    return _cloud_run


def create_cloud_logging_toolset() -> McpToolset | None:
    """Cloud Logging MCP — 6 tools: list_log_entries, list_log_names, etc."""
    global _cloud_logging, _cloud_logging_checked
    if _cloud_logging_checked:
        return _cloud_logging
    _cloud_logging_checked = True
    if not GCP_MCP_ENABLED:
        return None
    _cloud_logging = _create_toolset("https://logging.googleapis.com/mcp")
    return _cloud_logging


def create_compute_engine_toolset() -> McpToolset | None:
    """Compute Engine MCP — filtered to 8 read-only investigation tools."""
    global _compute_engine, _compute_engine_checked
    if _compute_engine_checked:
        return _compute_engine
    _compute_engine_checked = True
    if not GCP_MCP_ENABLED:
        return None
    _compute_engine = _create_toolset(
        "https://compute.googleapis.com/mcp",
        tool_filter=COMPUTE_ENGINE_TOOL_FILTER,
    )
    return _compute_engine

"""Bifrost MCP Gateway integration for ADK agents.

Bifrost is the standard MCP gateway for all acme agents.
It provides access to Azure DevOps, Datadog, Sentry, Atlassian, and other
internal tools via a single HTTP endpoint with virtual API keys.

Bifrost exposes META-TOOLS (executeToolCode, listToolFiles, readToolFile,
getToolDocs) — NOT the downstream tools directly. Agents use executeToolCode
to invoke downstream tools (Datadog, Sentry, ADO, etc.) through Bifrost's
sandboxed execution environment.

Usage:
    from .tools.bifrost_mcp import create_bifrost_toolset, truncate_tool_response

    toolset = create_bifrost_toolset()

    root_agent = LlmAgent(
        tools=[toolset, ...other_tools],
        after_tool_callback=truncate_tool_response,
    )
"""

import json
import logging
from typing import Any

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from ..config import get_bifrost_settings

logger = logging.getLogger(__name__)

MAX_TOOL_RESPONSE_CHARS = 4_000

# Connection tuning for the Bifrost StreamableHTTP MCP session.
# ADK defaults (timeout=5s, sse_read_timeout=300s) are wrong for a serverless
# Agent Engine: 5s is too tight for cold-start handshakes, and a 300s read
# timeout means a non-2xx/hung gateway stalls the whole run for 5 minutes
# before surfacing. Fail fast instead.
BIFROST_CONNECT_TIMEOUT = 30.0
BIFROST_SSE_READ_TIMEOUT = 60.0


def _unwrap_exception_group(exc: BaseException) -> list[BaseException]:
    """Recursively collect leaf exceptions from an ExceptionGroup tree.

    ADK wraps MCP session failures in an anyio TaskGroup whose
    BaseExceptionGroup stringifies as "unhandled errors in a TaskGroup
    (N sub-exceptions)" — hiding the real cause (timeout, 401, connect error,
    stale session). This walks the tree so the actual leaf is logged.
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_unwrap_exception_group(sub))
        return leaves
    return [exc]


class _LoggingMcpToolset(McpToolset):
    """McpToolset that logs the real leaf exception when get_tools() fails.

    Without this, a Bifrost session-create failure only appears in Cloud
    Logging as the opaque TaskGroup wrapper. This surfaces the root cause,
    then re-raises so ADK's existing handling (return [] + WARNING) proceeds.
    """

    async def get_tools(self, readonly_context=None):  # type: ignore[override]
        try:
            return await super().get_tools(readonly_context=readonly_context)
        except BaseException as exc:
            leaves = _unwrap_exception_group(exc)
            for i, leaf in enumerate(leaves, 1):
                logger.error(
                    "Bifrost get_tools failed (leaf %d/%d): %s: %s",
                    i,
                    len(leaves),
                    type(leaf).__name__,
                    leaf,
                    exc_info=leaf,
                )
            raise


def _truncate_value(value: Any, max_chars: int = MAX_TOOL_RESPONSE_CHARS) -> str:
    """Convert any value to string and truncate if needed."""
    if value is None:
        return "null"

    # Convert to string for measurement
    try:
        # Try JSON serialization for structured types
        if isinstance(value, (dict, list)):
            text = json.dumps(value, default=str)
        else:
            text = str(value)
    except Exception:
        # Fallback to str() if JSON serialization fails
        text = str(value)

    # Return as-is if under limit
    if len(text) <= max_chars:
        return text

    # Truncate with informative suffix
    truncated = text[:max_chars]
    suffix = f"\n\n... [TRUNCATED — response was {len(text):,} chars, showing first {max_chars:,}]"
    return truncated + suffix


def truncate_tool_response(
    tool: Any,
    args: dict[str, Any],
    tool_context: Any,
    tool_response: dict | str | Any,
) -> dict | str | None:
    """After-tool callback that truncates large tool responses to prevent token overflow.

    Handles ANY response type — str, dict, list, Pydantic models, ADK internal types.
    Returns None if no truncation needed (response already under limit).

    Prevents token overflow (observed: 1.36M tokens > 1M max) when Bifrost
    tools return large JSON payloads (full monitor lists, log dumps, etc.).
    """
    if not tool_response:
        return None

    # Handle string responses
    if isinstance(tool_response, str):
        text_version = _truncate_value(tool_response)
        # Return None if no truncation happened (original was under limit)
        return text_version if len(text_version) < len(tool_response) + 100 else None

    # Handle dict responses (common ADK format with "result" key)
    if isinstance(tool_response, dict):
        result = tool_response.get("result")
        if result is None:
            # Dict without "result" key — serialize entire dict
            text_version = _truncate_value(tool_response)
            original_length = len(json.dumps(tool_response, default=str))
            if len(text_version) < original_length:
                return {"result": text_version}
            return None

        # Dict with "result" key — truncate just the result
        truncated_result = _truncate_value(result)
        try:
            original_length = len(json.dumps(result, default=str)) if not isinstance(result, str) else len(result)
        except Exception:
            original_length = len(str(result))

        if len(truncated_result) < original_length:
            return {**tool_response, "result": truncated_result}
        return None

    # Handle all other types (list, Pydantic models, ADK internal types, etc.)
    # Convert to string, truncate, return as dict
    text_version = _truncate_value(tool_response)
    try:
        # Measure original size
        if isinstance(tool_response, list):
            original_length = len(json.dumps(tool_response, default=str))
        else:
            original_length = len(str(tool_response))
    except Exception:
        original_length = len(str(tool_response))

    # Only return truncated version if truncation actually happened
    if len(text_version) < original_length:
        return {"result": text_version}

    return None


def build_tool_list(*tools: Any) -> list:
    """Build a tool list, filtering out None values from disabled toolsets."""
    return [t for t in tools if t is not None]


_bifrost_token_checked = False
_bifrost_has_token = False


def create_bifrost_toolset() -> McpToolset | None:
    """Return a PER-AGENT MCP toolset connected to the Bifrost gateway.

    Each agent gets its OWN McpToolset (own MCP session). A shared singleton
    serializes all agents' tool calls onto one session: timing proved the
    Tier-1 barrier ballooned to ~384s under Haiku (agents queuing on the one
    session) vs ~142s on Sonnet, because Haiku fires tool calls fast enough to
    collide constantly. Per-agent sessions let the 9 Tier-1 agents actually run
    concurrently. Direct testing confirmed the Bifrost gateway serves many
    concurrent sessions fine (9/9 in <1s), so this is safe. The get_tools
    handshake is still cached+serialized (see _LoggingMcpToolset) so the ADK
    concurrent-get_tools issue stays contained.

    Returns None when BIFROST_API_TOKEN is not set, preventing agents from
    hallucinating tool responses against an unauthenticated connection.
    """
    global _bifrost_token_checked, _bifrost_has_token

    settings = get_bifrost_settings()
    if not _bifrost_token_checked:
        _bifrost_token_checked = True
        _bifrost_has_token = bool(settings.api_token)
    if not _bifrost_has_token:
        return None

    params = StreamableHTTPConnectionParams(
        url=settings.mcp_url,
        headers={
            "Authorization": f"Bearer {settings.api_token}",
            # Required by the MCP StreamableHTTP spec; some gateways 4xx without it.
            "Accept": "application/json, text/event-stream",
        },
        timeout=BIFROST_CONNECT_TIMEOUT,
        sse_read_timeout=BIFROST_SSE_READ_TIMEOUT,
    )

    # Expose only the meta-tools agents actually need. readToolFile (dumps full
    # tool source) was pure discovery overhead — telemetry (2026-07-08) showed
    # every Bifrost agent calling it ~3x/scenario, ~31% of Bifrost calls were
    # discovery cruft. Drop it so agents CAN'T waste calls on it; keep getToolDocs
    # (one orientation call) + listToolFiles (unknown-tool fallback) + executeToolCode
    # (the actual data calls — never restricted, per accuracy constraint).
    return _LoggingMcpToolset(
        connection_params=params,
        tool_filter=["executeToolCode", "getToolDocs", "listToolFiles"],
    )

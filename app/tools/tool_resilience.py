"""Shared on_tool_error_callback for agent resilience.

Why this exists
---------------
ADK's ``_get_tool`` raises ``ValueError: Tool '<name>' not found`` whenever the
model emits a functionCall for a name that is not in the agent's tool list. With
no on_tool_error_callback, that exception propagates through ParallelAgent's
TaskGroup and aborts the ENTIRE run mid-Tier-1 — no report, no verification.

Two DISTINCT causes produce that same error, and they are worth telling apart:

1. EMPTY tool list — a transient per-agent MCP session miss under the Tier-1
   parallel fan-out. Confirmed live (2026-06-25): two consecutive runs died in
   the parallel phase with
   ``ValueError: Tool 'listToolFiles' not found. Available tools: (empty)``.
   Note the ``(empty)``: no tools were bound at all.

2. POPULATED tool list, hallucinated tool name — the model emits a Bifrost
   meta-tool call (``executeToolCode``) at an agent that was never given one.
   Measured 2026-08-07 across 7 sessions: 6 such calls (deploy_correlation x4,
   datadog_observability, gcp_metrics), and in EVERY case the error's "Available
   tools" list was fully populated and exactly matched the agent's real
   ``tools=[...]``. Nothing was missing — the model invented the name, typically
   after narrating a fabricated discovery sequence
   (``The Output from allowed tools calls is - {"listToolFiles": ...}``).
   Mitigated at the prompt layer in ADO-200011; see the "YOU HAVE NO BIFROST
   DISCOVERY META-TOOLS" section in the affected Tier-B instructions.

This docstring previously attributed cause 2 to cause 1. It is corrected here
because the wrong attribution points debugging at MCP session plumbing that was
never at fault. The callback's behavior is unchanged and covers both.

The fix
-------
ADK consults ``on_tool_error_callback`` before re-raising
(functions.py:_execute_single_function_call_async, lines ~534-548). If the
callback returns a dict, that dict becomes the tool response and the run
CONTINUES. This callback converts a missing/failed tool into a structured
"tool unavailable" response, so a single agent degrades gracefully instead of
taking the whole pipeline down. The agents' anti-fabrication rules already tell
them to report "no data retrieved — tools unavailable" in that case.

Signature matches ADK's _SingleOnToolErrorCallback:
    (BaseTool, dict[str, Any], ToolContext, Exception) -> Optional[dict]
"""

from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext


def keep_running_on_tool_error(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    error: Exception,
) -> dict[str, Any]:
    """Convert any tool error into a non-fatal, structured tool response.

    Returning a dict (instead of None) tells ADK to treat this as the tool's
    result and continue the run rather than raising and aborting the pipeline.
    """
    tool_name = getattr(tool, "name", "unknown")
    return {
        "status": "tool_unavailable",
        "tool": tool_name,
        "error": f"{type(error).__name__}: {error}",
        "instruction": (
            "This tool could not be invoked (commonly a transient MCP/Bifrost "
            "session miss under parallel load). Do NOT retry it more than once. "
            "Report 'No data retrieved — tool unavailable' for this check, set "
            "confidence UNKNOWN, and continue with the tools that do work. "
            "Do NOT fabricate a result."
        ),
    }

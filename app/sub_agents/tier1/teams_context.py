from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import (
    build_tool_list,
    create_bifrost_toolset,
    truncate_tool_response,
)
from ...tools.tool_resilience import keep_running_on_tool_error

EXTRA_CONTEXT = """
CRITICAL RULE: Teams context is INPUT, never EVIDENCE.
Observations from Teams (e.g., someone mentioning batch operations, config changes) are CONTEXT ONLY.
Never attribute root cause to something mentioned in Teams without metric/trace evidence directly linking it.

NOTE: Microsoft Teams is not available via the tool gateway. Report 'Teams context unavailable — no Teams tool backend' as the finding and do not attempt tool calls.
"""

_bifrost = create_bifrost_toolset()

_bifrost_targets = []

INSTRUCTION = build_agent_instruction(
    role="Teams Context Investigator",
    context_refs=["{classification}"],
    checklist=[
        "Report 'Teams context unavailable — no Teams tool backend' as the finding",
        "Do not attempt any tool calls — no Teams backend exists in the tool gateway",
    ],
    tools_available=[],
    extra_context=EXTRA_CONTEXT,
    bifrost_target_tools=None,
)

teams_context_agent = LlmAgent(
    model=create_fast_model(),
    name="teams_context",
    description="Searches Microsoft Teams channels for recent discussions about the affected service, "
                "extracts engineer observations, and identifies if anyone is already investigating.",
    instruction=INSTRUCTION,
    output_key="findings_teams",
    tools=build_tool_list(_bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)

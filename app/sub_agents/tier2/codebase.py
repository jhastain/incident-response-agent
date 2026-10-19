"""Codebase Search Agent — Tier 2 last resort agent.

Only dispatched when Tier 1 + other Tier 2 agents cannot identify root cause.
This agent is token-heavy and searches ADO code repositories for error patterns.
"""

from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import (
    build_tool_list,
    create_bifrost_toolset,
    truncate_tool_response,
)
from ...tools.tool_resilience import keep_running_on_tool_error

CHECKLIST = [
    "Look up the repository for the affected service by name using repo_get_repo_by_name_or_id",
    "If a specific PR ID is known from classification or synthesis, fetch it using repo_get_pull_request_by_id",
    "NOTE: Full-text code search is NOT available — only repo/PR-by-id lookup exists. Do NOT attempt to grep source.",
    "If the relevant code cannot be inspected via these tools, report 'code search unavailable — only repo/PR-by-id lookup exists' and degrade gracefully",
]

_bifrost = create_bifrost_toolset()

_bifrost_targets = ["repo_get_repo_by_name_or_id", "repo_get_pull_request_by_id"]

INSTRUCTION = build_agent_instruction(
    role="Codebase Search Agent",
    context_refs=["{classification}", "{synthesis}"],
    checklist=CHECKLIST,
    tools_available=[],
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
    extra_context="""\
## Codebase Search-Specific Rules
- Default ADO project: AcmeProjects
- Look up the repository matching the service name from the classification using repo_get_repo_by_name_or_id
- If service name is 'acme.example' or 'WebCore', look up the 'WebCore' repository
- If service name is 'files.acme.example', look up the 'WebCore' repository (FileService is part of WebCore monolith)
- IMPORTANT: There is NO full-text code search tool available. Do NOT claim to grep or search file contents.
- If a specific PR ID is referenced in classification or synthesis, fetch it with repo_get_pull_request_by_id
- If neither tool yields useful information, report 'code search unavailable — only repo/PR-by-id lookup exists'

### Available Tool Capabilities
- repo_get_repo_by_name_or_id: Look up a repo by name or ID (not full-text search)
- repo_get_pull_request_by_id: Fetch a specific PR by known ID only

### Output Priority
Return ONLY the most relevant findings. Include:
- Repository metadata (name, default branch, last updated)
- PR details if fetched (title, description, files changed, author)
- If tools were insufficient, clearly state degraded mode and why
""",
)

codebase_agent = LlmAgent(
    model=create_fast_model(),
    name="codebase_agent",
    description="Searches ADO code repositories for error messages, recent changes, and code patterns related to the incident.",
    instruction=INSTRUCTION,
    output_key="findings_codebase",
    tools=build_tool_list(_bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)

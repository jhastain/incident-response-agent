"""Tier 1 parallel phase, gated per-run by the category's decision tree.

Membership is still the static 9. Each member carries a `before_agent_callback`
that reads `tier1_plan` (written by `tree_router`) and either runs or skips with
a sentinel.

Gating in a callback rather than rebuilding the ParallelAgent per run, because:
  - ADK enforces single parenting. Adding one BaseAgent to two ParallelAgents
    raises `ValueError: Agent 'shared' already has a parent agent`. Per-run
    assembly would need agent clones (new model clients and MCP toolsets each
    run) or mutation of `parent_agent`.
  - Dynamic construction inside this ParallelAgent has already failed twice here
    -- "ContextVar was created in a different Context", and children aclose()d
    mid-await wedging the whole phase (see datadog_tierb.py).

Returning Content from `before_agent_callback` is ADK's supported skip: it sets
`end_invocation` on that agent's own model_copy'd branch context, so
`_run_async_impl` never runs and siblings are unaffected. Verified against
google-adk 2.5.0 (pinned in uv.lock); re-check if that version moves.

A skipped agent costs its scheduling slot only -- no model call, no tokens.
"""

from google.adk.agents import ParallelAgent

from ...tier1_routing import (
    ALL_TIER1_AGENTS,
    TIER1_AGENT_FINDINGS_KEYS,
    TIER1_FOCUS_KEY_PREFIX,
    skip_sentinel,
)
from .cloud_run_health_tierb import cloud_run_health_tierb
from .compute_engine_health import compute_engine_health_agent
from .datadog_tierb import datadog_tierb
from .deploy_correlation_tierb import deploy_correlation_tierb
from .gcp_logging_tierb import gcp_logging_tierb
from .gcp_metrics_tierb import gcp_metrics_tierb
from .known_issues_tierb import known_issues_tierb
from .sentry_tierb import sentry_tierb
from .teams_context import teams_context_agent

# The `?` suffix is ADK's optional-state marker: a missing key renders empty
# rather than raising KeyError, so a step with no action still builds a prompt.
_FOCUS_SECTION = """

## Category-Specific Investigation Focus (from the decision tree for this alert's category)

{{{key}?}}

This is the step an SRE tuned for THIS category, and it is where to start. It
does NOT replace your checklist or your evidence rules: investigate the focus
first, then continue with anything else the alert justifies, and report what the
tools actually returned. An empty focus above just means the tree named no
specific action for you -- proceed with your normal checklist.
"""


def _skip_callback(agent_name: str):
    """Build the before_agent_callback that gates one Tier 1 agent."""
    findings_key = TIER1_AGENT_FINDINGS_KEYS[agent_name]

    def gate(callback_context):
        plan = callback_context.state.get("tier1_plan")
        # No plan means tree_router did not run; fail open.
        if not isinstance(plan, dict) or not plan.get("selected"):
            return None
        if agent_name in plan["selected"]:
            return None

        from google.genai import types  # local: keep module import cost off cold start

        sentinel = skip_sentinel(agent_name, plan)
        # AC 5: an unset findings key is indistinguishable from "found nothing".
        callback_context.state[findings_key] = sentinel
        return types.Content(role="model", parts=[types.Part(text=sentinel)])

    return gate


_TIER1_AGENTS = [
    cloud_run_health_tierb,
    compute_engine_health_agent,
    gcp_logging_tierb,
    gcp_metrics_tierb,
    datadog_tierb,
    sentry_tierb,
    deploy_correlation_tierb,
    teams_context_agent,
    known_issues_tierb,
]

# Diverging from the routing tables would mean an agent that can never be
# selected, or a plan naming one that does not exist. Fail at import instead.
_names = {agent.name for agent in _TIER1_AGENTS}
if _names != set(ALL_TIER1_AGENTS):
    raise RuntimeError(
        "tier1_parallel membership does not match app/tier1_routing.py. "
        f"only in parallel.py: {sorted(_names - set(ALL_TIER1_AGENTS))}; "
        f"only in tier1_routing.py: {sorted(set(ALL_TIER1_AGENTS) - _names)}"
    )

for _agent in _TIER1_AGENTS:
    _agent.before_agent_callback = _skip_callback(_agent.name)
    # Appended, not woven in, so no existing prompt text moves (AC 3).
    _agent.instruction = _agent.instruction + _FOCUS_SECTION.format(
        key=TIER1_FOCUS_KEY_PREFIX + _agent.name
    )

tier1_parallel = ParallelAgent(
    name="tier1_parallel",
    description=(
        "Runs the Tier 1 investigation agents concurrently. Membership is the full 9; "
        "the category's decision tree gates which of them actually investigate."
    ),
    sub_agents=_TIER1_AGENTS,
)

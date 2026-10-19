"""Fetches the category's decision tree and builds the Tier 1 plan.

Runs after `infra_context` and before Tier 1 (AC 1) so its output can gate
execution. Code-only: one Firestore read plus a pure function, because a model
on the critical path in front of the Tier 1 barrier would cost more than the
routing saves.

Writes:
  tier1_plan            the plan dict from build_tier1_plan
  tier1_routing_note    one-line summary rendered into the report
  tier1_focus_<agent>   the tree's `action` text, read as `{tier1_focus_<agent>?}`

Adds a median 0.39s to the sequential path (5 live reads: 0.57/0.39/0.36/0.39/0.44s).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from ..tier1_routing import TIER1_FOCUS_KEY_PREFIX, build_tier1_plan, routing_note
from ..tools.firestore_tools import get_triage_guidance

logger = logging.getLogger(__name__)


def _as_dict(value) -> dict:
    """Classification arrives as a dict under the runner; be tolerant anyway."""
    if isinstance(value, dict):
        return value
    return {}


class TreeRouterAgent(BaseAgent):
    """Reads decision_trees/<category> and turns it into a Tier 1 execution plan."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        classification = _as_dict(ctx.session.state.get("classification"))
        category = str(classification.get("category", "") or "unknown").strip() or "unknown"

        # get_triage_guidance returns its own errors as JSON, but client
        # construction (missing GOOGLE_CLOUD_PROJECT, no ADC) raises out of it.
        # Either way the run continues on all 9 agents (AC 7).
        guidance_json: str | None
        try:
            guidance_json = get_triage_guidance(category)
        except Exception as exc:
            logger.error(
                "Decision tree lookup raised for category %r (%s: %s) -- "
                "failing open to all Tier 1 agents",
                category, type(exc).__name__, exc,
            )
            guidance_json = None

        plan = build_tier1_plan(category, guidance_json, classification)
        note = routing_note(plan)

        if plan.get("fail_open"):
            logger.warning("Tier 1 routing failed open: %s", note)
        else:
            logger.info("Tier 1 routing: %s", note)

        # Flat keys, not a nested dict: a prompt cannot index into one (AC 3).
        state_delta: dict = {"tier1_plan": plan, "tier1_routing_note": note}
        for agent, action in plan.get("focus", {}).items():
            state_delta[f"{TIER1_FOCUS_KEY_PREFIX}{agent}"] = action

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=note)]),
            actions=EventActions(state_delta=state_delta),
        )


tree_router_agent = TreeRouterAgent(
    name="tree_router",
    description=(
        "Reads the category's Firestore decision tree and builds the Tier 1 execution "
        "plan: which agents run, what each investigates, and why the rest were skipped."
    ),
)

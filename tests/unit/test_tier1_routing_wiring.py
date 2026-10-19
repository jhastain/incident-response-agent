"""Tier 1 routing through the real ADK runner (ADO-200023).

test_tier1_routing.py covers the plan as a pure function; this covers what can
only be wrong at runtime. Chiefly: `before_agent_callback` returning Content sets
`ctx.end_invocation`, whose name suggests it ends the whole invocation. It does
not -- ParallelAgent gives each sub-agent a `model_copy` of the context, so the
flag is per-branch. That is an ADK implementation detail a version bump could
change, and the failure would be quiet and total: one skipped agent aborting the
whole investigation.

Stand-in agents mirror the Tier 1 contract (one findings key each) without models
or MCP toolsets.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from google.adk.agents import BaseAgent, ParallelAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.sub_agents.tier1.parallel import _skip_callback
from app.tier1_routing import ALL_TIER1_AGENTS, TIER1_AGENT_FINDINGS_KEYS


class _FakeTier1(BaseAgent):
    """Stands in for a Tier 1 LlmAgent: writes its findings key, nothing else."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        key = TIER1_AGENT_FINDINGS_KEYS[self.name]
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=f"{self.name} investigated")]),
            actions=EventActions(state_delta={key: f"REAL FINDINGS from {self.name}"}),
        )


class _Tail(BaseAgent):
    """Stands in for synthesis: proves the pipeline continued past Tier 1."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text="synthesised")]),
            actions=EventActions(state_delta={"synthesis": "ran"}),
        )


def _run(plan, agent_names):
    """Run a mini pipeline with `plan` pre-seeded, return (authors, final state)."""

    async def main():
        agents = []
        for name in agent_names:
            agent = _FakeTier1(name=name)
            agent.before_agent_callback = _skip_callback(name)
            agents.append(agent)

        root = SequentialAgent(
            name="mini_pipeline",
            sub_agents=[
                ParallelAgent(name="tier1_parallel_stub", sub_agents=agents),
                _Tail(name="synthesis_stub"),
            ],
        )
        runner = InMemoryRunner(app=App(root_agent=root, name="routing_probe"))
        session = await runner.session_service.create_session(
            app_name="routing_probe", user_id="u", state={"tier1_plan": plan}
        )
        authors = []
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="alert")]),
        ):
            authors.append(event.author)
        final = await runner.session_service.get_session(
            app_name="routing_probe", user_id="u", session_id=session.id
        )
        return authors, dict(final.state)

    return asyncio.run(main())


PLAN = {
    "category": "sql-database",
    "selected": ["gcp_logging", "datadog_observability"],
    "skipped": [
        {"agent": "sentry_error", "reason": "not in the Tier 1 selection for category 'sql-database'"},
        {"agent": "teams_context", "reason": "skip rule fired -- environment == 'dev' -- no Teams backend"},
    ],
    "focus": {"gcp_logging": "Search for AppSqlException and deadlock events"},
    "tree_version": 4,
    "tree_source": "tree",
    "fail_open": False,
    "notes": [],
}

AGENTS = ["gcp_logging", "datadog_observability", "sentry_error", "teams_context"]


class TestSkippingThroughTheRunner:
    def test_selected_agents_produce_real_findings(self):
        _, state = _run(PLAN, AGENTS)
        assert state["findings_gcp_logging"] == "REAL FINDINGS from gcp_logging"
        assert state["findings_datadog"] == "REAL FINDINGS from datadog_observability"

    def test_a_skipped_agent_never_enters_its_run_impl(self):
        """No model call, no tool call -- the point of skipping."""
        _, state = _run(PLAN, AGENTS)
        assert "REAL FINDINGS" not in state["findings_sentry"]

    def test_a_skipped_agents_findings_key_holds_the_sentinel(self):
        """AC 5. An unset key renders as "" and reads as 'checked, found nothing'."""
        _, state = _run(PLAN, AGENTS)
        assert state["findings_sentry"].startswith("NOT RUN")
        assert state["findings_teams"].startswith("NOT RUN")

    def test_the_sentinel_carries_the_skip_reason(self):
        _, state = _run(PLAN, AGENTS)
        assert "no Teams backend" in state["findings_teams"]

    def test_the_sentinel_names_the_tree_version(self):
        """AC 6."""
        _, state = _run(PLAN, AGENTS)
        assert "v4" in state["findings_sentry"]

    def test_every_findings_key_is_populated_one_way_or_the_other(self):
        """No silent gap: synthesis must never see an unset key."""
        _, state = _run(PLAN, AGENTS)
        for name in AGENTS:
            assert state.get(TIER1_AGENT_FINDINGS_KEYS[name]), name

    def test_skipping_does_not_end_the_invocation(self):
        """end_invocation is per-branch. If it leaked, synthesis would not run."""
        authors, state = _run(PLAN, AGENTS)
        assert state.get("synthesis") == "ran"
        assert "synthesis_stub" in authors

    def test_a_skipped_agent_does_not_take_its_siblings_down(self):
        authors, _ = _run(PLAN, AGENTS)
        assert "gcp_logging" in authors
        assert "datadog_observability" in authors

    def test_a_skipped_agent_still_emits_an_event(self):
        """Visible in the trace as a decision, not as an agent that vanished."""
        authors, _ = _run(PLAN, AGENTS)
        assert "sentry_error" in authors

    def test_skipping_the_first_agent_works(self):
        """Order-independence: the gate is per-agent, not positional."""
        plan = {**PLAN, "selected": ["datadog_observability"],
                "skipped": [{"agent": "gcp_logging", "reason": "r"}]}
        _, state = _run(plan, ["gcp_logging", "datadog_observability"])
        assert state["findings_gcp_logging"].startswith("NOT RUN")
        assert state["findings_datadog"].startswith("REAL")

    def test_skipping_every_agent_still_reaches_synthesis(self):
        """Should be unreachable via build_tier1_plan (AC 8), but must not wedge."""
        plan = {**PLAN, "selected": ["nobody"],
                "skipped": [{"agent": n, "reason": "r"} for n in AGENTS]}
        _, state = _run(plan, AGENTS)
        assert state.get("synthesis") == "ran"


class TestFailOpenThroughTheRunner:
    def test_a_fail_open_plan_runs_every_agent(self):
        """AC 7, at the gate rather than in the planner."""
        plan = {**PLAN, "selected": AGENTS, "skipped": [], "fail_open": True,
                "tree_source": "tree-unavailable", "tree_version": None}
        _, state = _run(plan, AGENTS)
        for name in AGENTS:
            assert state[TIER1_AGENT_FINDINGS_KEYS[name]].startswith("REAL"), name

    @pytest.mark.parametrize("plan", [None, {}, {"selected": []}, "not a dict", 42])
    def test_a_missing_or_junk_plan_runs_every_agent(self, plan):
        """tree_router did not run, or wrote something unusable. Investigate anyway."""
        _, state = _run(plan, AGENTS)
        for name in AGENTS:
            assert state[TIER1_AGENT_FINDINGS_KEYS[name]].startswith("REAL"), name


class TestTheRealPipelineIsWiredInOrder:
    """AC 1: the lookup runs after infra_context and before Tier 1."""

    def test_tree_router_precedes_tier1(self):
        from app.agent import root_agent

        names = [a.name for a in root_agent.sub_agents]
        assert names.index("tree_router") < names.index("tier1_with_history")

    def test_tree_router_follows_infra_context(self):
        from app.agent import root_agent

        names = [a.name for a in root_agent.sub_agents]
        assert names.index("infra_context") < names.index("tree_router")

    def test_tree_router_is_not_inside_the_tier1_parallel_barrier(self):
        """The old placement -- inside historical_context -- is what made it useless."""
        from app.agent import tier1_with_history

        assert "tree_router" not in {a.name for a in tier1_with_history.sub_agents}

    def test_historical_context_is_still_parallel_with_tier1(self):
        """Its ~35s parallelization is preserved; this change must not undo it."""
        from app.agent import tier1_with_history

        assert {a.name for a in tier1_with_history.sub_agents} == {
            "historical_context", "tier1_parallel"
        }

    def test_every_tier1_agent_carries_a_gate(self):
        from app.sub_agents.tier1.parallel import tier1_parallel

        for agent in tier1_parallel.sub_agents:
            assert agent.before_agent_callback is not None, agent.name


class TestTreeRouterFailsOpenWhenTheLookupRaises:
    """TreeRouterAgent's `except Exception` branch, the last defence for AC 7.

    get_triage_guidance returns its own Firestore errors as JSON, so the only way
    in is a failure before the query -- missing GOOGLE_CLOUD_PROJECT or absent
    ADC. Both are ordinary misconfigured-deploy states, and a wrong branch kills
    the investigation at the router instead of running all 9.

    Patch `app.sub_agents.tree_router.get_triage_guidance`, the name the module
    bound at import; patching firestore_tools leaves that reference intact.
    """

    def _run_router(self, monkeypatch, fake_lookup):
        from app.sub_agents import tree_router as tr

        monkeypatch.setattr(tr, "get_triage_guidance", fake_lookup)

        async def main():
            runner = InMemoryRunner(
                app=App(root_agent=tr.tree_router_agent, name="router_probe")
            )
            session = await runner.session_service.create_session(
                app_name="router_probe",
                user_id="u",
                # A category that WOULD narrow to fewer than 9 if the lookup
                # succeeded, so a pass cannot be explained by an unroutable category.
                state={"classification": {"category": "sql-database",
                                          "severity": "sev2", "environment": "prod"}},
            )
            async for _ in runner.run_async(
                user_id="u",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="alert")]),
            ):
                pass
            final = await runner.session_service.get_session(
                app_name="router_probe", user_id="u", session_id=session.id
            )
            return dict(final.state)

        return asyncio.run(main())

    def test_the_fixture_proves_this_category_would_otherwise_narrow(self):
        """If sql-database stopped narrowing, everything below would pass for the
        wrong reason."""
        from app.tier1_routing import DEFAULT_TIER1_SELECTION

        assert len(DEFAULT_TIER1_SELECTION["sql-database"]) < len(ALL_TIER1_AGENTS)

    def test_a_raising_lookup_runs_all_nine_agents(self, monkeypatch):
        def boom(category):
            raise KeyError("GOOGLE_CLOUD_PROJECT")

        plan = self._run_router(monkeypatch, boom)["tier1_plan"]

        assert plan["fail_open"] is True
        assert plan["selected"] == list(ALL_TIER1_AGENTS)
        assert plan["skipped"] == []

    def test_a_raising_lookup_does_not_propagate(self, monkeypatch):
        """An exception escaping here kills the whole SequentialAgent pipeline."""
        def boom(category):
            raise RuntimeError("google.auth: could not determine credentials")

        state = self._run_router(monkeypatch, boom)

        assert state.get("tier1_plan") is not None
        assert "FAIL-OPEN" in state["tier1_routing_note"]

    def test_the_note_says_it_failed_open(self, monkeypatch):
        """AC 6 / AC 7: a degraded run must be legible, not silently complete."""
        def boom(category):
            raise KeyError("GOOGLE_CLOUD_PROJECT")

        note = self._run_router(monkeypatch, boom)["tier1_routing_note"]

        assert "tree-unavailable" in note
        assert "skipped: none" in note

    def test_no_focus_keys_are_written_when_the_lookup_raises(self, monkeypatch):
        """A stale focus would misdirect an agent with text from another run."""
        from app.tier1_routing import TIER1_FOCUS_KEY_PREFIX

        def boom(category):
            raise KeyError("GOOGLE_CLOUD_PROJECT")

        state = self._run_router(monkeypatch, boom)

        assert not [k for k in state if k.startswith(TIER1_FOCUS_KEY_PREFIX)]

    def test_a_normal_lookup_on_the_same_path_still_narrows(self, monkeypatch):
        """Control: proves the rig can observe a non-fail-open outcome."""
        import json

        def ok(category):
            return json.dumps({
                "category": category,
                "version": 11,
                "triage_steps": [
                    {"order": 1, "agent": "gcp_logging_agent", "tier": 1,
                     "action": "Search for AppSqlException", "hit_rate": 0.9},
                ],
            })

        plan = self._run_router(monkeypatch, ok)["tier1_plan"]

        assert plan["fail_open"] is False
        assert plan["tree_version"] == 11
        assert len(plan["selected"]) < len(ALL_TIER1_AGENTS)

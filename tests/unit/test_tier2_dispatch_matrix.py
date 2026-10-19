"""Regression tests for Tier 2 dispatch routing.

Context — the 2026-07-28 five-run live campaign (.e2e5) raised two suspected
dispatch defects. Investigation showed both were measurement artifacts:

  1. "tier2_dispatcher never fired on A5 (cloud-infrastructure)."
     Not a defect. cloud-infrastructure maps to an EMPTY agent list, so the
     correct behaviour is to call nothing. The dispatcher ran and emitted the
     "No Tier 2 investigation needed" text — it simply made no tool calls, and
     the scorer derives its agent list from tool calls only.

  2. "deploy_correlation fired on A5 but not A3."
     Not a dispatch defect. deploy_correlation is an always-on Tier 1 member of
     tier1_parallel, never conditionally dispatched. It ran in all five runs.

These tests pin the invariants that make the above true, so a future edit that
silently turns cloud-infrastructure into a dispatching category, or that moves
deploy_correlation out of always-on Tier 1, fails loudly.
"""

from app.categories import CATEGORIES, TIER2_DISPATCH_MATRIX
from app.sub_agents.tier2_dispatcher import DISPATCHER_INSTRUCTION, tier2_dispatcher

# Categories that are deliberately Tier 1 only. A5 (IBD TEST instances unhealthy)
# classified as cloud-infrastructure, which is why nothing dispatched.
#
# `monitoring-meta` was in this set until 2026-08-07 and is deliberately no longer:
# a real outage misclassified into it hit an empty mapping, so no agent could produce
# lock evidence and the pipeline fabricated a trigger. See TestMonitoringMetaSafetyNet.
TIER1_SUFFICIENT = {"cloud-infrastructure", "unknown"}


class TestMatrixIntegrity:
    def test_every_category_has_a_matrix_entry(self):
        assert set(TIER2_DISPATCH_MATRIX) == set(CATEGORIES)

    def test_matrix_only_references_real_tier2_agents(self):
        available = {tool.agent.name for tool in tier2_dispatcher.tools}
        referenced = {a for agents in TIER2_DISPATCH_MATRIX.values() for a in agents}
        assert referenced <= available

    def test_no_category_lists_a_duplicate_agent(self):
        for category, agents in TIER2_DISPATCH_MATRIX.items():
            assert len(agents) == len(set(agents)), category


class TestTier1SufficientCategories:
    """The A5 case: an empty mapping is correct restraint, not a misroute."""

    def test_cloud_infrastructure_dispatches_nothing(self):
        assert TIER2_DISPATCH_MATRIX["cloud-infrastructure"] == []

    def test_tier1_sufficient_categories_are_empty(self):
        for category in TIER1_SUFFICIENT:
            assert TIER2_DISPATCH_MATRIX[category] == []

    def test_prompt_tells_the_model_to_call_nothing_for_empty_categories(self):
        assert "NO Tier 2 agents (Tier 1 sufficient)" in DISPATCHER_INSTRUCTION
        assert "Call NO agent tools in this case" in DISPATCHER_INSTRUCTION

    def test_every_other_category_does_dispatch(self):
        """Guards the opposite regression — silently emptying a real mapping."""
        for category, agents in TIER2_DISPATCH_MATRIX.items():
            if category not in TIER1_SUFFICIENT:
                assert agents, category


class TestMonitoringMetaSafetyNet:
    """monitoring-meta must not be a routing dead end.

    On 2026-07-30 a real acme.example outage (17:30-17:39 UTC) that the DBAs were
    already calling blocking was classified `monitoring-meta`. That category mapped to
    an empty list, so `sql_dba_agent` never ran and no agent could produce hard lock
    evidence. With nothing to reason from, the pipeline named a fabricated trigger —
    a Hangfire job that targets Redis, not SQL Server, and ran ~41 hours earlier.

    The category is for "the monitoring system itself is broken", so a DBA agent is
    not the natural fit. It is here as a misroute safety net: the correct fix is
    classifier scope, but routing must degrade safely until that lands.
    """

    def test_monitoring_meta_dispatches_sql_dba(self):
        assert TIER2_DISPATCH_MATRIX["monitoring-meta"] == ["sql_dba_agent"]

    def test_monitoring_meta_is_not_empty(self):
        """The specific regression: an empty list here is what caused the red herring."""
        assert TIER2_DISPATCH_MATRIX["monitoring-meta"]


class TestDispatchKeysOnCategoryOnly:
    """A5 classified service=unknown / env=dev. Neither can affect dispatch."""

    def test_prompt_exposes_only_classification_and_synthesis(self):
        assert "{classification}" in DISPATCHER_INSTRUCTION
        assert "{synthesis}" in DISPATCHER_INSTRUCTION

    def test_dispatch_matrix_is_not_keyed_on_service_or_environment(self):
        for category, agents in TIER2_DISPATCH_MATRIX.items():
            assert isinstance(category, str)
            assert isinstance(agents, list)

    def test_unknown_service_cannot_suppress_a_dispatching_category(self):
        """service=unknown is orthogonal to routing — scheduled-jobs still maps
        to sql_dba_agent regardless of which service was resolved (A3=WebCore,
        A4=CoreSvc both dispatched sql_dba_agent)."""
        assert TIER2_DISPATCH_MATRIX["scheduled-jobs"] == ["sql_dba_agent"]


class TestObservedLiveRunsMatchTheMatrix:
    """The Tier 2 tool calls actually observed in the .e2e5 campaign."""

    OBSERVED = {
        "A1": ("deploy-release", ["codebase_agent"]),
        "A2": ("deploy-release", ["codebase_agent"]),
        "A3": ("scheduled-jobs", ["sql_dba_agent"]),
        "A4": ("scheduled-jobs", ["sql_dba_agent"]),
        "A5": ("cloud-infrastructure", []),
    }

    def test_each_run_dispatched_exactly_what_the_matrix_maps(self):
        for run, (category, dispatched) in self.OBSERVED.items():
            assert TIER2_DISPATCH_MATRIX[category] == dispatched, run


class TestDeployCorrelationIsAlwaysOnTier1:
    """Defect #2 rested on deploy_correlation being conditionally dispatched."""

    def test_deploy_correlation_is_not_a_tier2_dispatch_target(self):
        referenced = {a for agents in TIER2_DISPATCH_MATRIX.values() for a in agents}
        assert not any("deploy_correlation" in a for a in referenced)

    def test_deploy_correlation_is_a_member_of_tier1_parallel(self):
        from app.sub_agents.tier1.parallel import tier1_parallel

        names = {sub.name for sub in tier1_parallel.sub_agents}
        assert any("deploy_correlation" in n for n in names), names

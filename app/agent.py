"""Incident Response Agent — root agent and App for Agent Engine deployment."""

from google.adk.agents import ParallelAgent, SequentialAgent
from google.adk.apps import App

from .app_utils.dd_run_plugin import DatadogRunPlugin
from .evaluators.verification_bridge import submit_verification_evaluations
from .sub_agents.classifier import alert_classifier
from .sub_agents.historical_context import historical_context_agent
from .sub_agents.infra_context import infra_context_agent
from .sub_agents.persistence import persistence_agent
from .sub_agents.report import report_agent
from .sub_agents.synthesis import synthesis_agent
from .sub_agents.tier1.parallel import tier1_parallel
from .sub_agents.tier2_dispatcher import tier2_dispatcher
from .sub_agents.tree_router import tree_router_agent
from .sub_agents.verification import verification_agent

# Only infra_context must precede Tier 1 — the Tier 1 agents read {service_context}
# from it. historical_context produces {past_incidents}, which is consumed by
# synthesis/report (never by Tier 1), so it runs concurrently WITH Tier 1 instead of
# blocking before it. This removes ~35s of historical_context latency from the
# sequential critical path (it overlaps the Tier 1 barrier), reclaiming time for the
# report/verification/persistence tail against the fixed ~600s streamQuery deadline.
#
# tree_router sits in front of this barrier and spends ~0.4s of that back (one
# Firestore read, no LLM). End-to-end effect is unmeasured: a narrowed Tier 1
# only saves wall time when it drops the phase's slowest member (AC 9).
tier1_with_history = ParallelAgent(
    name="tier1_with_history",
    description="Runs the Tier 1 investigation agents concurrently with historical-context lookup.",
    sub_agents=[historical_context_agent, tier1_parallel],
)

root_agent = SequentialAgent(
    name="incident_response_orchestrator",
    description=(
        "Incident Response Agent — a multi-agent system that investigates production alerts "
        "across GCP, Datadog, Sentry, and Azure DevOps. Classifies alerts, loads historical "
        "and infrastructure context, runs parallel investigations, synthesizes findings, "
        "conditionally dispatches specialist agents, generates a Teams-ready report, "
        "verifies investigation quality and completeness, and persists the results to "
        "GCS and Firestore for future reference."
    ),
    sub_agents=[
        alert_classifier,
        infra_context_agent,
        # AC 1: after infra_context, before Tier 1, so the tree can gate execution.
        tree_router_agent,
        tier1_with_history,
        synthesis_agent,
        tier2_dispatcher,
        report_agent,
        verification_agent,
        persistence_agent,
    ],
    # Ships verification_agent's scores to Datadog as trace-level evaluations.
    # No-ops when Datadog is disabled or the trace was not sampled.
    after_agent_callback=submit_verification_evaluations,
)

app = App(
    root_agent=root_agent,
    name="app",
    # Per-sub-agent cost/token spans and run dimensions; ddtrace's ADK
    # auto-instrumentation emits neither.
    plugins=[DatadogRunPlugin()],
)

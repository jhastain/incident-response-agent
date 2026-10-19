"""ADO-200027: three defects found soaking 287187 + 287188 in test.

Each test here was verified red against the revert -- the fix removed, the test
failing -- before being committed. A green test that has never gone red is not
evidence of anything.
"""

from __future__ import annotations

from app.sub_agents.persistence import _build_incident
from app.tier1_routing import build_tier1_plan, skip_sentinel
from app.tools.decision_tree_learning import score_contributions


def _sentinel() -> str:
    plan = build_tier1_plan("sql-database", None, {})
    plan.setdefault("skipped", [{"agent": "teams_context", "reason": "not selected"}])
    return skip_sentinel("teams_context", plan)


class TestSkippedAgentIsNotAMiss:
    """A routed-out agent scored (0 hits, 1 sample), pushing its rate to .4545.

    Self-reinforcing: skip an agent -> it looks useless -> skip it harder. The
    findings key is PRESENT for a skipped agent, so the absence check missed it.
    """

    def test_a_skip_sentinel_is_omitted_not_scored(self):
        incident = {
            "findings": {
                "findings_teams": _sentinel(),
                "findings_sentry": "Saw `WEBCORE-DOTNET-3C4D` in the trace",
            },
            "report": "Root cause tied to `WEBCORE-DOTNET-3C4D`",
            "synthesis": "",
            "alert_info": {"raw_text": "error rate high"},
        }
        scored = score_contributions(incident)
        assert "teams_context_agent" not in scored
        assert scored["sentry_agent"] is True

    def test_an_agent_that_ran_and_found_nothing_still_scores_a_miss(self):
        # The control: omission must mean "never ran", not "no artifact".
        incident = {
            "findings": {"findings_teams": "Checked Teams. No matching discussion."},
            "report": "unrelated",
            "synthesis": "",
            "alert_info": {"raw_text": ""},
        }
        assert score_contributions(incident)["teams_context_agent"] is False


class TestReportKnowsWhatNotRunMeans:
    """report.py had zero mention of NOT RUN while receiving sentinel-bearing
    findings -- so a skipped source could be reported as checked and clean."""

    def test_the_prompt_defines_the_sentinel(self):
        from app.sub_agents.report import REPORT_INSTRUCTION

        assert "NOT RUN -- skipped by decision tree" in REPORT_INSTRUCTION
        assert "NEVER QUERIED" in REPORT_INSTRUCTION

    def test_no_dangling_empty_section_header(self):
        from app.sub_agents.report import REPORT_INSTRUCTION

        assert "**Optional (from the tree router):**" not in REPORT_INSTRUCTION


class TestRoutingIsPersisted:
    """tree_router logs the note at INFO, which never reaches Cloud Logging
    (root is WARNING). The stored incident is the only record of a narrowed run."""

    def _state(self, **extra):
        state = {
            "classification": {"category": "sql-database", "severity": "SEV3"},
            "alert_raw_text": "db latency high",
            "report": "x",
        }
        state.update(extra)
        return state

    def test_plan_and_note_reach_the_incident(self):
        import datetime

        plan = {"selected": ["gcp_logging"], "skipped": [{"agent": "teams_context"}]}
        _, _, incident = _build_incident(
            self._state(tier1_plan=plan, tier1_routing_note="ran: gcp_logging"),
            datetime.datetime(2026, 8, 25, tzinfo=datetime.UTC),
        )
        assert incident["tier1_routing"]["plan"] == plan
        assert incident["tier1_routing"]["note"] == "ran: gcp_logging"

    def test_absent_when_the_router_never_wrote_anything(self):
        import datetime

        _, _, incident = _build_incident(
            self._state(),
            datetime.datetime(2026, 8, 25, tzinfo=datetime.UTC),
        )
        assert incident["tier1_routing"] is None

"""Regression tests for the missing-anchor fallback (ADO-200014).

Measured live 2026-08-07 against 5 real Platform-Alerts channel posts: 4 of 5 produced
`alert_timestamp: "unknown"`. The classifier was RIGHT to — the only timestamps those
alerts carry are RESOLVED and "NOTE ADDED by <human>" times, which the classifier
deliberately refuses to anchor on (anchoring on a resolve time is ADO-200003).

Leaving it unknown was still wrong. The Tier-1 prompts said "fall back to the last
2 hours" and the model has no clock, so on run 1 it invented a window and queried
`2024-12-20T22:00:00Z` — the wrong YEAR — returning 0 entries, which is
indistinguishable from a healthy service.

The fallback anchors on run_start_epoch, stamped by the classifier at t0. That is a
real clock and a genuine upper bound: the alert fired before the run investigating it.
It is labelled APPROXIMATE so nothing mistakes it for ground truth.
"""

import datetime as _dt

from app.sub_agents.classifier import _backfill_alert_timestamp, extract_epoch_timestamp
from app.sub_agents.tier1.gcp_logging_tierb import (
    INSTRUCTION as GCP_LOGGING_INSTRUCTION,
)

# Verbatim from the live Platform-Alerts channel, 2026-08-07. Trimmed to the parts that
# carry (or fail to carry) a time.
ALERT_NO_TIME_AT_ALL = (
    "Incomplete tasks with steps over 10 minutes:\n\n"
    "Deploy WebCore - Web App release 1000004-rc to Test - Blue "
    "(https://acme.octopus.app/app#/Spaces-1/tasks/ServerTasks-1000003)\n"
    " - Task State: Executing\n - Task Duration: 36 minute(s)\n"
    " - Active Step(s):\n   - Step 49: Deploy APIv1 Cloud Run\n"
    "      - Duration: 30 minute(s), 38 second(s)\nManual Intervention Needed @Sam Ortega"
)

ALERT_RESOLVED_TIME_ONLY = (
    "RESOLVED #1001: AcmeInfrastructure - Teardown Pipeline has a high failure rate\n"
    "https://acme.pagerduty.com/incidents/QEXAMPLE000003\n"
    "Service: Platform - Datadog - Low\nTriggered By: Datadog\n"
    "Monitor: https://us5.datadoghq.com/monitors/1142595\n"
    "RESOLVED by Datadog - Non-Prod - SRE - Datadog | 2026-08-07 at 12:54:49Z"
)

ALERT_NOTE_ADDED_TIME_ONLY = (
    "TRIGGERED #1004: Collect ADO Pool Metrics did not run\n"
    "Monitor: https://us5.datadoghq.com/monitors/10000001\n"
    "NOTE ADDED by Dana Okafor | 2026-08-07 at 03:39:06Z"
)

RUN_START = _dt.datetime(2026, 8, 7, 22, 15, 52, tzinfo=_dt.UTC)


class _Ctx:
    """Minimal stand-in for the ADK callback context."""

    def __init__(self, state, user_text=""):
        self.state = state
        self.user_content = type(
            "C", (), {"parts": [type("P", (), {"text": user_text})()]}
        )()


def _run(alert_text, classification, run_start=RUN_START):
    state = {
        "classification": classification,
        "run_start_epoch": run_start.timestamp() if run_start else None,
    }
    _backfill_alert_timestamp(_Ctx(state, alert_text))
    return state["classification"]


class TestResolveTimesAreStillRefused:
    """The pre-existing guarantee. Loosening this reintroduces ADO-200003."""

    def test_resolved_time_is_not_extracted_as_the_anchor(self):
        assert extract_epoch_timestamp(ALERT_RESOLVED_TIME_ONLY) == ""

    def test_note_added_time_is_not_extracted_as_the_anchor(self):
        """Alert 4's only timestamp is when a human commented on the ticket."""
        assert extract_epoch_timestamp(ALERT_NOTE_ADDED_TIME_ONLY) == ""

    def test_the_resolve_time_never_becomes_the_anchor(self):
        out = _run(ALERT_RESOLVED_TIME_ONLY, {"alert_timestamp": "unknown"})
        assert "2026-08-07T12:54:49" not in out["alert_timestamp"]


class TestFallbackFires:
    def test_unknown_becomes_the_run_start_time(self):
        out = _run(ALERT_NO_TIME_AT_ALL, {"alert_timestamp": "unknown"})
        assert out["alert_timestamp"] == "2026-08-07T22:15:52Z"

    def test_the_anchor_is_labelled_approximate(self):
        """A reader and the verifier must both be able to discount it."""
        out = _run(ALERT_NO_TIME_AT_ALL, {"alert_timestamp": "unknown"})
        assert out["alert_timestamp_source"].startswith("APPROXIMATE")

    def test_the_source_explains_why_a_present_timestamp_was_refused(self):
        out = _run(ALERT_RESOLVED_TIME_ONLY, {"alert_timestamp": "unknown"})
        src = out["alert_timestamp_source"]
        assert "RESOLVED/ACK/NOTE" in src
        assert "not date it" in src

    def test_empty_string_also_triggers_the_fallback(self):
        out = _run(ALERT_NO_TIME_AT_ALL, {"alert_timestamp": ""})
        assert out["alert_timestamp"] == "2026-08-07T22:15:52Z"

    def test_no_run_start_falls_back_to_the_explicit_missing_marker(self):
        """Never silently invent one. If there is no clock, say so."""
        out = _run(ALERT_NO_TIME_AT_ALL, {"alert_timestamp": "unknown"}, run_start=None)
        assert out["alert_timestamp_source"].startswith("MISSING")
        assert out["alert_timestamp"] == "unknown"


class TestRealAnchorsStillWin:
    """The fallback must never displace a genuine trigger time."""

    def test_epoch_param_beats_the_fallback(self):
        alert = "Datadog monitor https://us5.datadoghq.com/x?from_ts=1785443700000"
        out = _run(alert, {"alert_timestamp": "unknown"})
        assert out["alert_timestamp"] == "2026-07-30T20:35:00Z"
        assert out["alert_timestamp_source"] == "epoch-param-backfill"

    def test_a_model_extracted_time_is_kept_not_overwritten(self):
        """Alert 5 carried a real failure time; the model found it. Leave it alone."""
        out = _run(ALERT_NO_TIME_AT_ALL, {"alert_timestamp": "2026-08-06T20:13:00Z"})
        assert out["alert_timestamp"] == "2026-08-06T20:13:00Z"
        assert out["alert_timestamp_source"].startswith("model-asserted")


class TestPromptsNoLongerAskTheModelToGuess:
    def test_gcp_logging_forbids_inventing_a_date(self):
        assert "NEVER invent a date" in GCP_LOGGING_INSTRUCTION

    def test_gcp_logging_no_longer_says_fall_back_to_the_last_2_hours(self):
        """That instruction is what produced the 2024-12 query — the model has no clock."""
        assert 'unknown", fall back to the last 2 hours' not in GCP_LOGGING_INSTRUCTION

    def test_gcp_logging_tells_the_model_how_to_treat_an_approximate_anchor(self):
        assert "APPROXIMATE" in GCP_LOGGING_INSTRUCTION

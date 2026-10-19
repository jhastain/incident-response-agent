"""alert_timestamp must come from the alert, never from the model's guess.

Measured 2026-07-28 across a 5-alert live campaign: the classifier emitted
alert_timestamp 2025-08-19 / 2025-08-20 for two 2026-07-27 incidents — a year
off. The wrong year came from the stale example window baked into Datadog's
"associated traces" help link (start=1724077659428 -> 2024-08-19), which sits in
the alert body BEFORE the real link_event_ts. Three other alerts carried no time
at all and every query anchored on "now".

A confidently WRONG date is worse than a missing one: "unknown" trips the
missing-window guard, while a wrong date sails through and the whole report reads
as self-consistent about the wrong day.
"""

from __future__ import annotations

from app.sub_agents.classifier import extract_epoch_timestamp

# Verbatim fragment of the real A3/A4 Datadog->PagerDuty alert body: the help link
# carries a 2024 example window, the real trigger time is the later link_event_ts.
STALE_EXAMPLE = (
    "Please check the [associated traces]( https://us5.datadoghq.com/apm/traces"
    "?query=env%3Aprod&start=1724077659428&end=1724078559428&paused=false )."
)
REAL_TRIGGER = (
    "[Monitor Status](https://us5.datadoghq.com/monitors/1064301"
    "?from_ts=1784665701000&to_ts=1784666901000&link_event_ts=1784666601)"
)


class TestStaleExampleWindowIsNotTheAlertTime:
    def test_stale_2024_example_alone_yields_nothing(self):
        """The help-link window is boilerplate, not evidence. Better to return
        nothing (which trips the missing-window guard) than a 2024 date."""
        assert extract_epoch_timestamp(STALE_EXAMPLE) == ""

    def test_real_trigger_wins_when_both_present(self):
        got = extract_epoch_timestamp(STALE_EXAMPLE + " " + REAL_TRIGGER)
        assert got.startswith("2026-07-"), got
        assert "2024" not in got and "2025" not in got

    def test_from_ts_is_preferred_over_event_marks(self):
        """from_ts/start_ts anchor the window; prefer them over to_ts/event_ts."""
        got = extract_epoch_timestamp(REAL_TRIGGER)
        assert got == "2026-07-21T20:28:21Z", got


class TestExplicitTimeFieldsWhenNoEpochParam:
    """PagerDuty always supplies a Created time; the Teams harness supplies the
    channel post time. Without these, Power Automate / Octopus cards carry no
    timestamp at all and every downstream query anchors on NOW."""

    def test_pagerduty_created_field(self):
        text = "Status: triggered | Created: 2026-07-27T20:08:05Z | https://x"
        assert extract_epoch_timestamp(text) == "2026-07-27T20:08:05Z"

    def test_teams_post_time(self):
        text = "Alert time: 2026-07-28 15:30:38Z --- Incomplete tasks over 10 minutes"
        assert extract_epoch_timestamp(text) == "2026-07-28T15:30:38Z"

    def test_offset_is_normalised_to_utc(self):
        text = "Triggered at: 2026-07-27T15:08:05-05:00"
        assert extract_epoch_timestamp(text) == "2026-07-27T20:08:05Z"

    def test_unlabelled_dates_are_ignored(self):
        """Alert bodies are full of date-shaped strings (release ids, example
        links). Only an explicitly labelled time counts."""
        text = "Deploy WebCore release 1000002-rc, build 2026-01-15, step 52"
        assert extract_epoch_timestamp(text) == ""

    def test_no_time_anywhere_yields_empty(self):
        text = "Incomplete tasks with steps over 10 minutes: Deploy WebCore - Web App"
        assert extract_epoch_timestamp(text) == ""


class TestEpochOverridesTheModel:
    """The callback must OVERRIDE a model-supplied timestamp, not merely backfill
    'unknown'. The 2025-08-19 regression happened because the model was confident,
    so the old 'only fix if unknown' guard never fired."""

    def test_extractor_beats_the_models_wrong_year_on_real_alert_text(self):
        body = STALE_EXAMPLE + " " + REAL_TRIGGER
        recovered = extract_epoch_timestamp(body)
        model_said = "2025-08-19T22:30:01Z"
        assert recovered != model_said
        assert recovered.startswith("2026-07-")

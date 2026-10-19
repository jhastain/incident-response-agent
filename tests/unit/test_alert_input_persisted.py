"""The agent must persist the alert it was actually given.

Measured 2026-07-30 against the live dev corpus: `alert_info.raw_text` is null in
319 of 480 incident documents (66.5%), in a clean regression starting 2026-07-14
(last run with text 05:27:31 UTC, first without 15:07:09 UTC, zero overlap).

Mechanism: `_build_incident` read `classif.get("raw_text")`, but the classifier's
structured-output schema `AlertClassification` declares seven fields and raw_text
is not among them — so the lookup returns None on every run of the current
pipeline. The alert text is never captured anywhere.

Two consequences, both load-bearing for this project:

1. Those runs cannot be replayed, deduplicated by alert identity, or scored
   against ground truth. Accuracy is unmeasurable without the input.

2. The GCS filename derives from the same null (`desc = _slug(raw_text or
   category)`), so it degenerates to
   `<date>_sev<n>_<category>_<service>_<category>.json`. Replaying the real
   function over the 319 affected runs yields 123 distinct names — 196 runs
   (61.4%) silently overwrite an earlier one. Confirmed live: a read failed with
   `404 generation=1785447456381223: No such object` because the object was
   replaced between listing and reading.

The fix sources the alert from the invocation's own user content and puts
incident_id in the filename, so collisions are structurally impossible rather
than merely unlikely.
"""

from __future__ import annotations

import datetime

from app.sub_agents.persistence import _build_incident

NOW = datetime.datetime(2026, 7, 30, 21, 27, 50, tzinfo=datetime.UTC)

ALERT = (
    "[Datadog] WebCore line-item update error spike — 29,877 error spans, HTTP 500, "
    '"Sequence contains no elements" on /api/orderorderitems/update'
)

# What the classifier actually emits: seven fields, no raw_text.
CLASSIFICATION = {
    "category": "application-errors",
    "severity": "sev2",
    "service_name": "WebCore",
    "gcp_project": "acme-production-100000",
    "environment": "prod",
    "error_message": "Sequence contains no elements",
    "alert_timestamp": "2026-07-30T20:15:00Z",
}


class TestAlertTextIsPersisted:
    def test_raw_text_survives_when_classifier_does_not_echo_it(self):
        """The classifier has no raw_text field; the alert must still be stored."""
        incident, _, _ = _build_incident(
            {"classification": CLASSIFICATION, "alert_raw_text": ALERT}, NOW
        )
        assert incident["alert_info"]["raw_text"] == ALERT

    def test_missing_alert_text_is_null_not_the_category(self):
        """Absent input stays absent. Substituting the category would fabricate a
        record of an alert that was never received."""
        incident, _, _ = _build_incident({"classification": CLASSIFICATION}, NOW)
        assert incident["alert_info"]["raw_text"] is None


class TestFilenamesDoNotCollide:
    def test_two_runs_of_the_same_alert_get_different_files(self):
        """Same alert, same category, same service, same day — distinct objects.

        This is the 61.4% overwrite case. The two runs differ only in wall-clock
        time, which is exactly what incident_id carries.
        """
        later = NOW + datetime.timedelta(minutes=7)
        state = {"classification": CLASSIFICATION, "alert_raw_text": ALERT}

        _, first, _ = _build_incident(state, NOW)
        _, second, _ = _build_incident(state, later)

        assert first != second

    def test_filename_carries_the_incident_id(self):
        """The id is the collision-proof component; without it the name is a
        function of (date, sev, category, service) only — four fields that
        repeat constantly."""
        incident, file_name, _ = _build_incident(
            {"classification": CLASSIFICATION, "alert_raw_text": ALERT}, NOW
        )
        assert incident["incident_id"] in file_name
        assert file_name.endswith(".json")

    def test_filename_still_useful_without_alert_text(self):
        """Degraded input must not reintroduce the collapse: even with no alert
        text the name stays unique via incident_id."""
        _, first, _ = _build_incident({"classification": CLASSIFICATION}, NOW)
        _, second, _ = _build_incident(
            {"classification": CLASSIFICATION}, NOW + datetime.timedelta(seconds=3)
        )
        assert first != second

"""Regression tests for empty historical root causes (ADO-200005).

`find_similar_incidents` read `root_cause_analysis.summary` — a field
`persistence_agent` has never written. Verified against live Firestore on
2026-08-07: no agent-authored incident carries that key, so every result came back
with `root_cause: ""`. Historical context handed synthesis a list of incident ids
with no causes attached, and the agent treated each alert as the first of its kind.

The report shapes below are copied from real stored incidents in
`aiops-test-project`, not invented — the earlier archived fix targeted an older
template and would not have matched what the report agent emits today.
"""

from app.tools.firestore_tools import _root_cause_of

# Verbatim from incidents/20260807-203939-application-errors.
LIVE_BLOCK_REPORT = """\
**TL;DR:** WebCore schedule loading failed because SchedulesManager.GetCalendarList()
calls `.First()` on a zero-row worksite work-days query, returning 500.

**Root Cause (STRONG EVIDENCE):**
- Mechanism: `.First()` on empty work-days result throws, propagates as HTTP 500
- Trigger: alert text — `Mobile clients unable to load schedule items`
- Signal: [Datadog c5102fce](https://us5.datadoghq.com/error-tracking/issue/c5102fce)
- Change: Octopus — `Deployment to Test - Blue (1000004-rc) Complete`

**Key Findings:**
- Something else entirely
"""

# Verbatim from incidents/20260807-124505-cloud-infrastructure.
LIVE_BLOCK_REPORT_2 = """\
**Root Cause (STRONG EVIDENCE):**
- Mechanism: ADO pool-ID lookup failed, network unreachable — job logged ERROR by design
- Trigger: [GCP Alert](https://console.cloud.google.com/monitoring/alerting/alerts/0.ob5)
"""


class TestLiveReportShapes:
    """The shapes the deployed report agent actually produces."""

    def test_block_form_returns_the_mechanism_bullet(self):
        assert _root_cause_of({"report": LIVE_BLOCK_REPORT}) == (
            "`.First()` on empty work-days result throws, propagates as HTTP 500"
        )

    def test_second_live_report_also_resolves(self):
        got = _root_cause_of({"report": LIVE_BLOCK_REPORT_2})
        assert got.startswith("ADO pool-ID lookup failed")

    def test_mechanism_is_preferred_over_the_trigger_bullet(self):
        """Trigger/Signal/Change are corroboration; Mechanism is the causal claim."""
        got = _root_cause_of({"report": LIVE_BLOCK_REPORT})
        assert "Trigger" not in got
        assert "Datadog" not in got

    def test_extraction_stops_at_the_root_cause_section(self):
        got = _root_cause_of({"report": LIVE_BLOCK_REPORT})
        assert "Key Findings" not in got
        assert "Something else entirely" not in got


class TestOtherHeadingForms:
    def test_inline_bold_form(self):
        report = "**Root cause:** Connection pool exhausted under load\n\nMore text"
        assert _root_cause_of({"report": report}) == "Connection pool exhausted under load"

    def test_markdown_heading_with_bullets(self):
        report = "## Root Cause\n- Mechanism: Deadlock on dimension.SelectionChoiceLineItems\n"
        assert _root_cause_of({"report": report}) == (
            "Deadlock on dimension.SelectionChoiceLineItems"
        )

    def test_heading_without_mechanism_takes_first_bullet(self):
        report = "## Root Cause\n- Disk filled on the primary node\n- Secondary detail\n"
        assert _root_cause_of({"report": report}) == "Disk filled on the primary node"

    def test_case_insensitive(self):
        report = "**ROOT CAUSE:** Certificate expired"
        assert _root_cause_of({"report": report}) == "Certificate expired"


class TestLegacyFieldStillWins:
    def test_legacy_summary_is_preferred_when_present(self):
        """Older documents that do carry the field must keep working."""
        data = {
            "root_cause_analysis": {"summary": "Legacy recorded cause"},
            "report": LIVE_BLOCK_REPORT,
        }
        assert _root_cause_of(data) == "Legacy recorded cause"

    def test_empty_legacy_summary_falls_through_to_the_report(self):
        data = {"root_cause_analysis": {"summary": ""}, "report": LIVE_BLOCK_REPORT}
        assert _root_cause_of(data).startswith("`.First()`")


class TestHonestEmpty:
    """Returning "" is correct when there is nothing. Never invent a cause."""

    def test_no_report_and_no_legacy_field(self):
        assert _root_cause_of({}) == ""

    def test_report_with_no_root_cause_section(self):
        assert _root_cause_of({"report": "**Key Findings:**\n- Disk at 80%"}) == ""

    def test_new_format_report_resolves_from_the_mechanism_bullet(self):
        """ADO-200018: no Root Cause heading; the cause leads Key Findings."""
        report = "**Key Findings:**\n🔴 **STRONG EVIDENCE:**\n- Mechanism: pool exhausted\n"
        assert _root_cause_of({"report": report}) == "pool exhausted"

    def test_none_report(self):
        assert _root_cause_of({"report": None}) == ""

    def test_missing_root_cause_analysis_is_not_an_error(self):
        """The exact live condition: the key simply does not exist."""
        assert _root_cause_of({"service": "WebCore", "category": "sql-database"}) == ""

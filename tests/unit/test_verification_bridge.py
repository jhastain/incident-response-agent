"""verification_agent already scores every run. This maps those scores onto
Datadog evaluations without spending a single extra token.

The mapping has to survive what the verifier actually emits: an LLM that
sometimes fences its JSON, sometimes wraps it in prose, and sometimes returns
something unparseable. Persistence already learned this the hard way (0/11
scores persisted as null before _as_dict grew a fence-tolerant parser).

ADO-200019 dropped the `solution` dimension, so `ir_solution_actionability` is
no longer emitted. The label is not renamed or reused — its Datadog history
stays intact and simply stops receiving points.
"""

from __future__ import annotations

from app.evaluators.verification_bridge import build_evaluations

GOOD = {
    "diagnosis": "STRONG",
    "efficiency": "ADEQUATE",
    "automated_tier": "MEDIUM",
    "evidence_citations_valid": 8,
    "evidence_citations_invalid": 2,
    "reasoning": {
        "diagnosis_notes": "Root cause supported by GCP Logging + Datadog.",
        "efficiency_notes": "Some redundant queries.",
    },
}


class TestHappyPath:
    def test_emits_four_evaluations(self):
        assert len(build_evaluations(GOOD)) == 4

    def test_labels_are_stable_and_contain_no_dots(self):
        """The SDK rejects '.' in labels, and renaming a label orphans its
        history in Datadog. Both reasons to pin these in a test."""
        labels = {e["label"] for e in build_evaluations(GOOD)}
        assert labels == {
            "ir_diagnosis_quality",
            "ir_investigation_efficiency",
            "ir_evidence_citation_ratio",
            "ir_investigation_reliable",
        }
        assert not any("." in label for label in labels)

    def test_categorical_values_pass_through(self):
        by_label = {e["label"]: e for e in build_evaluations(GOOD)}
        assert by_label["ir_diagnosis_quality"]["value"] == "STRONG"
        assert by_label["ir_diagnosis_quality"]["metric_type"] == "categorical"

    def test_citation_ratio_is_valid_over_total(self):
        by_label = {e["label"]: e for e in build_evaluations(GOOD)}
        ratio = by_label["ir_evidence_citation_ratio"]
        assert ratio["metric_type"] == "score"
        assert ratio["value"] == 0.8

    def test_reliability_is_true_unless_unreliable(self):
        by_label = {e["label"]: e for e in build_evaluations(GOOD)}
        reliable = by_label["ir_investigation_reliable"]
        assert reliable["metric_type"] == "boolean"
        assert reliable["value"] is True
        assert reliable["assessment"] == "pass"

    def test_reasoning_notes_ride_along_for_triage(self):
        """A failing score with no reason is not actionable in the UI."""
        by_label = {e["label"]: e for e in build_evaluations(GOOD)}
        assert "GCP Logging" in by_label["ir_diagnosis_quality"]["reasoning"]


class TestSolutionDimensionIsGone:
    """ADO-200019. ADO-200018 deleted Recommended Actions from the report, so
    the solution dimension's MISSING value ("no solution or next steps
    proposed") became true of every report by construction. A dimension with
    one reachable value is worse than no dimension."""

    def test_no_solution_evaluation_is_emitted(self):
        labels = {e["label"] for e in build_evaluations(GOOD)}
        assert "ir_solution_actionability" not in labels

    def test_a_stale_verifier_still_emitting_solution_is_ignored(self):
        """The prompt no longer asks for it, but a model can emit an extra key.
        The bridge must not resurrect the Datadog label off a stray field."""
        payload = {**GOOD, "solution": "ACTIONABLE"}
        labels = {e["label"] for e in build_evaluations(payload)}
        assert "ir_solution_actionability" not in labels
        assert len(build_evaluations(payload)) == 4


class TestUnreliableRuns:
    def test_unreliable_tier_fails_the_boolean(self):
        payload = {**GOOD, "automated_tier": "UNRELIABLE"}
        by_label = {e["label"]: e for e in build_evaluations(payload)}
        assert by_label["ir_investigation_reliable"]["value"] is False
        assert by_label["ir_investigation_reliable"]["assessment"] == "fail"

    def test_zero_citations_scores_zero_not_a_crash(self):
        """No citations at all is the evidence_unavailable path — it must score
        0.0, not raise ZeroDivisionError and lose the whole submission."""
        payload = {**GOOD, "evidence_citations_valid": 0, "evidence_citations_invalid": 0}
        by_label = {e["label"]: e for e in build_evaluations(payload)}
        assert by_label["ir_evidence_citation_ratio"]["value"] == 0.0


class TestMalformedInput:
    def test_empty_dict_emits_nothing(self):
        assert build_evaluations({}) == []

    def test_missing_fields_are_skipped_not_faked(self):
        """Emitting a default for a score the verifier never produced would be
        fabricated data on a correctness dashboard."""
        labels = {e["label"] for e in build_evaluations({"diagnosis": "WEAK"})}
        assert labels == {"ir_diagnosis_quality"}

    def test_non_numeric_citation_counts_are_skipped(self):
        payload = {"evidence_citations_valid": "eight", "evidence_citations_invalid": 2}
        assert build_evaluations(payload) == []

    def test_unexpected_categorical_value_still_passes_through(self):
        """Datadog accepts any string for a categorical. Better to record what
        the verifier actually said than to drop it for failing our enum."""
        by_label = {e["label"]: e for e in build_evaluations({"diagnosis": "SPLENDID"})}
        assert by_label["ir_diagnosis_quality"]["value"] == "SPLENDID"

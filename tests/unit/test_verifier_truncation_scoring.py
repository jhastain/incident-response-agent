"""The verifier must not score a CUT result as an ABSENT one (ADO-200016).

Measured on the 5-run batch of 2026-08-07: 7 of 8 negative citation rulings were FALSE.
Adjudicated against raw session events, the cited values — 6,901 APM spans, a 184-point
series, a `context canceled` log line at a stated timestamp, four 200-status datapoints —
were all present in the real tool output and absent only from the truncated view the
verifier was given. Each was counted in `evidence_citations_invalid`, and those false
counts pulled tiers down.

ADO-200010 fixed truncation at the ITEM level; coverage is now 100% of calls. The
remaining loss is per-result: each result body is cut, and the verifier treated
"not in the part I was shown" as "not in the evidence".

Scope note: this file covers the SCORING rule only. The retrieval side is a separate,
larger problem — measured, a single run carries up to 259K chars of raw tool output
against a 60K prompt budget, so no truncation scheme can show it all and the verifier
needs a way to SEARCH rather than be handed a pre-cut view. Tracked on ADO-200016.
"""

from app.sub_agents.verification import VERIFICATION_INSTRUCTION


class TestCutIsNotAbsent:
    def test_a_truncated_result_is_not_scored_invalid(self):
        assert "NOT INVALID" in VERIFICATION_INSTRUCTION

    def test_truncated_citations_are_excluded_from_the_invalid_count(self):
        """A false invalid is what drags the tier down."""
        assert "exclude it from `evidence_citations_invalid`" in VERIFICATION_INSTRUCTION

    def test_there_is_a_distinct_label_for_this_case(self):
        assert "unverifiable_truncated" in VERIFICATION_INSTRUCTION

    def test_the_rule_is_stated_as_scoring_not_advice(self):
        """It existed as prose before and was violated on 7 of 8 rulings."""
        assert "this is a scoring rule, not advice" in VERIFICATION_INSTRUCTION.lower()


class TestTheThreeCasesAreDistinguished:
    """A ruling requires establishing WHICH case applies, not just "I didn't see it"."""

    def test_absent_call_is_invalid(self):
        assert "absent from the run entirely" in VERIFICATION_INSTRUCTION

    def test_complete_result_that_omits_the_value_is_invalid(self):
        assert "shown **complete**" in VERIFICATION_INSTRUCTION

    def test_truncated_result_is_the_third_case(self):
        assert "is **truncated**" in VERIFICATION_INSTRUCTION


class TestSearchBeforeRuling:
    def test_verifier_must_search_all_results_first(self):
        """In run 5 it inspected ~2 of 33 calls and concluded absence."""
        assert "SEARCH every shown `result_summary`" in VERIFICATION_INSTRUCTION

    def test_it_is_told_the_value_may_be_in_an_unexpected_call(self):
        assert "not just the" in VERIFICATION_INSTRUCTION
        assert "you did not anticipate" in VERIFICATION_INSTRUCTION


class TestAntiFabricationGateIntact:
    """None of the above may weaken the guarantee that fabrication is caught."""

    def test_a_genuinely_absent_call_is_still_invalid(self):
        assert "no entry for that tool → **INVALID**" in VERIFICATION_INSTRUCTION

    def test_a_contradicting_complete_result_is_still_invalid(self):
        assert "contradicts or\n   omits the value → **INVALID**" in VERIFICATION_INSTRUCTION

    def test_the_no_evidence_hard_gate_still_exists(self):
        assert "HARD GATE — no evidence means no score" in VERIFICATION_INSTRUCTION
        assert 'set `diagnosis` = "UNSUPPORTED" and `automated_tier` = "UNRELIABLE"' in (
            VERIFICATION_INSTRUCTION
        )

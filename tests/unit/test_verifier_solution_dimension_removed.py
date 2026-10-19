"""The verifier no longer scores a `solution` dimension (ADO-200019).

ADO-200018 deleted the `**Recommended Actions**` section from the report. The
solution rubric's MISSING value read "No solution or next steps proposed.
Report ends with diagnosis only." — which is now every report, by construction.
A scored dimension with exactly one reachable value is worse than no dimension:
it makes the verification record look richer than it is, and it invites the
model to launder a diagnosis-quality judgment through a solution-quality label.

THIS IS A SCORING-BEHAVIOUR CHANGE, NOT A NO-OP. Two measured facts drove the
tier-table rework and are pinned below.

FACT 1 — removing the dimension must not newly downgrade anything.
The old table keyed LOW on "WEAK or UNSUPPORTED diagnosis OR DISCONNECTED
solution", and UNRELIABLE on "UNSUPPORTED diagnosis AND (DISCONNECTED or
MISSING) solution". MISSING was never on its own a route to LOW, and it only
reached UNRELIABLE in conjunction with an already-UNSUPPORTED diagnosis. So a
report that is MISSING-by-construction never lost a tier under the old table
either — the new table must preserve that, and it does: UNSUPPORTED diagnosis
alone is now the UNRELIABLE route, which is exactly what the AND-clause
collapsed to once `solution` is pinned at MISSING.

FACT 2 — removing it REMOVES spurious downgrades that were never authorized.
Research over stored verification records found 21 of 41 poor tiers (51%) came
from a `VAGUE` solution ruling on a run whose diagnosis was STRONG or PARTIAL.
`VAGUE` appears nowhere in the old tier table — not under LOW, not under
UNRELIABLE. The model was downgrading outside its own table. Those runs are now
tiered on their diagnosis, which is what they should always have been tiered on.

Honesty note: these are PROMPT assertions. A unit test can prove the dimension
is absent from the instruction and that the tier table no longer references it.
It CANNOT prove the model stops inventing a solution-shaped complaint — that is
only measurable on a live batch.
"""

from app.evaluators.verification_bridge import build_evaluations
from app.sub_agents.verification import VERIFICATION_INSTRUCTION, verification_agent

_TIER_SECTION = VERIFICATION_INSTRUCTION.split("## Automated Tier Calculation", 1)[1].split(
    "## Evidence Citation Validation", 1
)[0]
# The tier RULES proper, excluding the "do not downgrade outside it" prose that
# follows them — that prose deliberately names the retired VAGUE grade in order
# to record the 51% spurious-downgrade finding.
_TIER_RULES = _TIER_SECTION.split("### The tier table is exhaustive", 1)[0]


class TestTheRubricNoLongerHasASolutionDimension:
    def test_the_solution_rubric_heading_is_gone(self):
        assert "Solution (Is the proposed fix actionable?)" not in VERIFICATION_INSTRUCTION

    def test_none_of_the_solution_grades_are_defined_anymore(self):
        for grade in ("**ACTIONABLE:**", "**VAGUE:**", "**DISCONNECTED:**", "**MISSING:**"):
            assert grade not in VERIFICATION_INSTRUCTION

    def test_the_one_reachable_value_is_no_longer_scoreable(self):
        """The exact MISSING text that ADO-200018 made universally true."""
        assert "No solution or next steps proposed" not in VERIFICATION_INSTRUCTION

    def test_efficiency_is_renumbered_as_the_second_dimension(self):
        assert "### 2. Efficiency (Was the investigation well-conducted?)" in (
            VERIFICATION_INSTRUCTION
        )
        assert "### 3. Efficiency" not in VERIFICATION_INSTRUCTION

    def test_the_agent_advertises_two_dimensions_not_three(self):
        assert "two dimensions" in VERIFICATION_INSTRUCTION
        assert "three dimensions" not in VERIFICATION_INSTRUCTION
        assert "solution" not in verification_agent.description


class TestTheTierTableKeysOnDiagnosisAlone:
    """The tier is now a function of the diagnosis score and the evidence."""

    def test_no_tier_rule_mentions_a_solution_grade(self):
        for grade in ("ACTIONABLE", "VAGUE", "DISCONNECTED", "MISSING"):
            assert grade not in _TIER_RULES

    def test_the_table_says_so_explicitly(self):
        assert (
            "**The tier is a function of the diagnosis score and the evidence, and of "
            "nothing else.**" in _TIER_RULES
        )

    def test_efficiency_alone_never_moves_the_tier(self):
        assert "Efficiency never moves the tier on its own" in _TIER_RULES

    def test_medium_still_requires_a_strong_diagnosis(self):
        medium = _TIER_RULES.split("**MEDIUM:**", 1)[1].split("**LOW:**", 1)[0]
        assert "STRONG diagnosis" in medium

    def test_unreliable_route_one_is_now_unsupported_diagnosis_alone(self):
        """FACT 1. The old route was 'UNSUPPORTED diagnosis AND (DISCONNECTED or
        MISSING) solution'. With solution pinned at MISSING by construction, that
        AND-clause collapses to its diagnosis half — so this is the SAME rule,
        not a broadened one."""
        unreliable = _TIER_RULES.split("**UNRELIABLE:**", 1)[1]
        assert "- UNSUPPORTED diagnosis\n" in unreliable

    def test_the_low_band_keeps_its_pre_existing_diagnosis_wording(self):
        """The old LOW rule was 'WEAK or UNSUPPORTED diagnosis OR DISCONNECTED
        solution'. Only the solution clause is removed; UNSUPPORTED already had a
        stronger home under UNRELIABLE, so WEAK is what LOW keys on. PARTIAL's
        placement is deliberately left exactly as the old prose had it — resolving
        that pre-existing ambiguity is a separate, unmeasured change."""
        low = _TIER_RULES.split("**LOW:**", 1)[1].split("**UNRELIABLE:**", 1)[0]
        assert "WEAK diagnosis" in low
        assert "PARTIAL" not in low
        assert "**A PARTIAL diagnosis can never yield UNRELIABLE.**" in _TIER_SECTION


class TestTheAbsentFixIsNotADowngrade:
    """FACT 2. The 51% of poor tiers that came from a VAGUE ruling."""

    def test_the_prompt_forbids_downgrading_for_a_missing_fix(self):
        assert (
            "**The absence of a fix, a remediation, or next steps is NOT a downgrade.**"
            in VERIFICATION_INSTRUCTION
        )

    def test_it_explains_why_every_report_now_reads_as_diagnosis_only(self):
        assert "The report has no\n  Recommended Actions section" in VERIFICATION_INSTRUCTION

    def test_it_blocks_laundering_the_downgrade_through_the_notes(self):
        assert "do NOT invent a\n  solution-shaped complaint" in VERIFICATION_INSTRUCTION

    def test_the_measured_spurious_downgrade_rate_is_recorded_in_the_prompt(self):
        assert "21 of 41 poor tiers (51%)" in VERIFICATION_INSTRUCTION


class TestTheOutputSchemaDropsTheField:
    def test_the_json_example_has_no_solution_key(self):
        schema = VERIFICATION_INSTRUCTION.split("## Output Format", 1)[1].split(
            "## Token Budget", 1
        )[0]
        assert '"solution"' not in schema
        assert '"solution_notes"' not in schema
        assert '"diagnosis"' in schema
        assert '"efficiency"' in schema

    def test_the_prompt_forbids_re_adding_the_field_under_a_synonym(self):
        """An LLM told to drop `solution` readily emits `next_steps`, which a
        downstream consumer would treat as a different field entirely."""
        assert "There is no `solution` field and no `solution_notes` field" in (
            VERIFICATION_INSTRUCTION
        )
        assert "`next_steps`, `remediation`, `fix_quality`" in VERIFICATION_INSTRUCTION


class TestUntouchedContracts:
    """This change must not disturb the keys other systems read."""

    def test_the_evidence_citation_keys_are_untouched(self):
        for key in (
            "evidence_citations_valid",
            "evidence_citations_invalid",
            "evidence_citations_unchecked",
            "evidence_coverage",
        ):
            assert key in VERIFICATION_INSTRUCTION

    def test_automated_tier_is_still_the_emitted_tier_key(self):
        """persistence.py derives combined_tier from it and
        firestore_tools.find_similar_incidents filters on that."""
        assert '"automated_tier"' in VERIFICATION_INSTRUCTION

    def test_the_diagnosis_dimension_is_untouched(self):
        assert "### 1. Diagnosis (Does evidence support the root cause claim?)" in (
            VERIFICATION_INSTRUCTION
        )
        for grade in ("**STRONG:**", "**PARTIAL:**", "**WEAK:**", "**UNSUPPORTED:**"):
            assert grade in VERIFICATION_INSTRUCTION

    def test_datadog_still_gets_the_other_four_evaluations(self):
        """Only ir_solution_actionability stops reporting."""
        labels = {
            e["label"]
            for e in build_evaluations(
                {
                    "diagnosis": "STRONG",
                    "efficiency": "ADEQUATE",
                    "automated_tier": "MEDIUM",
                    "evidence_citations_valid": 8,
                    "evidence_citations_invalid": 2,
                }
            )
        }
        assert labels == {
            "ir_diagnosis_quality",
            "ir_investigation_efficiency",
            "ir_evidence_citation_ratio",
            "ir_investigation_reliable",
        }

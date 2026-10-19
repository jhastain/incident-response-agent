"""The verifier must enumerate every citation and search each one atomically (ADO-200016).

Measured live 2026-08-10 on engine `1000000000000000001`, five runs, after
`search_session_evidence` shipped in PR 109787. The tool worked and the prompt drove it —
52 searches, complete evidence coverage every run — and the verifier failed anyway, in two
opposite directions with one shared cause.

DIRECTION 1 — it rejected real evidence. 27 of 52 searches came back `found: false`, and 24
of those were multi-token PROSE queries. Matching is case-insensitive substring, so a
description can never match text that does not contain it verbatim: `peak 39 agents` cannot
match `"max":39`. 26 of the 27 misses had an atomic value that WAS in the evidence. The
verifier then wrote "FABRICATED" about values sitting in plain sight; run 3 scored
1 valid / 8 invalid and was dragged to LOW on citations that were all real. False-INVALID
rate went 87.5% before the search tool -> 79-93% after. No improvement.

DIRECTION 2 — it accepted fabricated evidence. Run 5 ruled 0 citations invalid while its
report cited Cloud Run revision `1000003-rc`, which appears in NO tool output (the real
revisions present: 2293997, 2295839, 2296259, 2297394, 2298594, 2299958, 2305053), plus
Valkey figures 5.3% / 16.8% / 14.7% with no matching decimal anywhere (checked +/-0.05
across 387 decimals). It never searched for any of them.

ONE ROOT CAUSE: nothing forced the verifier to enumerate every citation and check each one.
It freelanced — picked what caught its attention, phrased queries however it liked. That
produces sloppy phrasing on what it does check AND blindness to what it skips.

The behavioural classes below pin the substring semantics with the REAL query strings from
the batch: each prose query genuinely misses and each atomic query genuinely hits, against
evidence built from the real values. The prompt classes assert the four instructions exist.

Honesty note: these are PROMPT changes. A unit test can prove the instruction is present and
that the tool behaves as the instruction describes. It CANNOT prove the model follows it —
the false-INVALID rate is only re-measurable on a live run.
"""

import json

from app.sub_agents.verification import VERIFICATION_INSTRUCTION
from app.tools.session_tools import _pair_calls_with_results, _render_search


def _call(name, args=None, call_id=None):
    return {
        "author": "tier1",
        "content": {
            "parts": [{"functionCall": {"id": call_id or name, "name": name, "args": args or {}}}]
        },
    }


def _result(name, payload, call_id=None):
    return {
        "author": name,
        "content": {
            "parts": [
                {"functionResponse": {"id": call_id or name, "name": name, "response": payload}}
            ]
        },
    }


def _search(events, query):
    return json.loads(_render_search(query, _pair_calls_with_results(events)))


def _evidence(body, tool="fetch_datadog", args=None):
    """One tool call whose result contains `body` verbatim, as the real payloads do."""
    return [
        _call(tool, args or {"query": "agent metrics"}, call_id="c0"),
        _result(tool, {"result": body}, call_id="c0"),
    ]


# The seven real misses from the 2026-08-10 batch: the prose query the verifier actually
# sent, the atomic query it should have sent, and the text genuinely present in the evidence.
REAL_MISSES = [
    ("peak 39 agents", "39", '{"max":39,"min":4,"avg":31.74871794871795}'),
    ("195 count", "195", '{"count":195,"series":"agent.runs"}'),
    ("31.7 avg", "31.7", '{"avg":31.74871794871795}'),
    (
        "ContainerHealthy 36.16s",
        "36.16",
        "reason=ContainerHealthy Containers became healthy in 36.16s.",
    ),
    ("1 active instance 0 idle", "idle", '{"active_instance_count":1,"idle_instance_count":0}'),
    ("38 agents max 39", "38", '{"current":38,"max":39}'),
    ("07:03 CDT 2026-08-07", "07:03", '{"ts":"2026-08-07T12:03:44Z","local":"07:03:44"}'),
]


class TestTheRealProseQueriesGenuinelyMiss:
    """Direction 1, measured. Each of these was a false INVALID against present evidence."""

    def test_every_prose_query_from_the_batch_returns_not_found(self):
        for prose, _atomic, body in REAL_MISSES:
            result = _search(_evidence(body), prose)

            assert result["found"] is False, (
                f"expected the prose query {prose!r} to miss against {body!r} — "
                "if it now hits, the substring semantics this fix assumes have changed"
            )

    def test_every_atomic_query_from_the_batch_hits_the_same_evidence(self):
        """26 of 27 misses had an atomic value that was in the evidence all along."""
        for prose, atomic, body in REAL_MISSES:
            result = _search(_evidence(body), atomic)

            assert result["found"] is True, (
                f"{atomic!r} (the atomic form of {prose!r}) must be findable in {body!r}"
            )
            assert atomic in result["matches"][0]["window"]

    def test_peak_39_agents_misses_but_39_finds_max_39(self):
        """The canonical case, spelled out: description vs. the JSON the tool emitted."""
        evidence = _evidence('{"max":39,"min":4}')

        assert _search(evidence, "peak 39 agents")["found"] is False
        hit = _search(evidence, "39")

        assert hit["found"] is True
        assert "39" in hit["matches"][0]["window"]
        assert hit["matches"][0]["tool"] == "fetch_datadog"

    def test_a_quoted_query_misses_because_results_are_json_serialized(self):
        """Searched text is `json.dumps(response)`, so an inner `"` is stored as `\\"`.

        A query of `"max":39` therefore misses the exact text it describes. The prompt
        forbids quote characters in queries for this reason.
        """
        evidence = _evidence('{"max":39,"min":4}')

        assert _search(evidence, '"max":39')["found"] is False
        assert _search(evidence, "max")["found"] is True
        assert _search(evidence, "39")["found"] is True

    def test_a_truncated_decimal_matches_as_a_prefix_but_not_with_its_label(self):
        """`31.7 avg` missed; `31.7` is a prefix of the stored 31.74871794871795."""
        evidence = _evidence('{"avg":31.74871794871795}')

        assert _search(evidence, "31.7 avg")["found"] is False
        assert _search(evidence, "31.7")["found"] is True

    def test_a_reformatted_timestamp_never_matches_verbatim(self):
        """The report renders CDT; the evidence stores UTC plus a local fragment."""
        evidence = _evidence('{"ts":"2026-08-07T12:03:44Z","local":"07:03:44"}')

        assert _search(evidence, "07:03 CDT 2026-08-07")["found"] is False
        assert _search(evidence, "07:03")["found"] is True


class TestFabricatedValuesStillComeBackAbsent:
    """Direction 2, measured. Run 5's revision id was never searched — when it IS, it misses."""

    REAL_REVISIONS = (
        "revisions: ir-agent-00-2293997, ir-agent-00-2295839, ir-agent-00-2296259, "
        "ir-agent-00-2297394, ir-agent-00-2298594, ir-agent-00-2299958, ir-agent-00-2305053"
    )

    def test_the_fabricated_revision_is_not_found_among_the_real_ones(self):
        result = _search(_evidence(self.REAL_REVISIONS, tool="fetch_cloud_run"), "1000003-rc")

        assert result["found"] is False
        assert result["match_count"] == 0
        assert result["results_searched"] == 1

    def test_a_real_revision_from_the_same_payload_is_found(self):
        """Proves the miss above is absence, not a broken fixture."""
        result = _search(_evidence(self.REAL_REVISIONS, tool="fetch_cloud_run"), "2295839")

        assert result["found"] is True

    def test_the_fabricated_valkey_percentages_are_not_found(self):
        """5.3 / 16.8 / 14.7 matched no decimal in the session, checked to +/-0.05."""
        body = '{"cpu_pct":4.1,"mem_pct":22.6,"hit_rate_pct":99.2,"evictions":0}'
        for fabricated in ("5.3", "16.8", "14.7"):
            assert _search(_evidence(body, tool="fetch_gcp_metrics"), fabricated)["found"] is False

    def test_absence_is_reported_as_evidence_of_absence_not_as_a_gap(self):
        result = _search(_evidence(self.REAL_REVISIONS, tool="fetch_cloud_run"), "1000003-rc")

        assert "evidence of absence" in result["instruction_to_verifier"]
        assert "INVALID" in result["instruction_to_verifier"]


class TestPromptRequiresEnumerationFirst:
    """Rule 1. The list comes first; the searching second."""

    def test_there_is_an_explicit_enumerate_before_ruling_step(self):
        assert "STEP 1 — ENUMERATE EVERY CITATION BEFORE YOU RULE ON ANY OF THEM" in (
            VERIFICATION_INSTRUCTION
        )

    def test_it_names_the_section_to_enumerate_from(self):
        # ADO-200018: the report has no Root Cause section; the committed cause is
        # the Mechanism bullet that leads Key Findings.
        assert "Key Findings" in VERIFICATION_INSTRUCTION
        assert "Mechanism bullet" in VERIFICATION_INSTRUCTION

    def test_it_names_the_citation_kinds_including_revisions_and_quoted_strings(self):
        for kind in ("every number", "every timestamp", "revision", "quoted string"):
            assert kind in VERIFICATION_INSTRUCTION

    def test_skipping_and_sampling_are_both_forbidden(self):
        assert "**No skipping.**" in VERIFICATION_INSTRUCTION
        assert "**No sampling.**" in VERIFICATION_INSTRUCTION

    def test_the_enumeration_step_precedes_searching_in_the_execution_steps(self):
        steps = VERIFICATION_INSTRUCTION.split("## Execution Steps", 1)[1]
        enumerate_at = steps.index("ENUMERATE every citation")
        search_at = steps.index("search_session_evidence")

        assert enumerate_at < search_at

    def test_it_cites_the_measured_run_that_motivated_the_rule(self):
        assert "1000000000000000001" in VERIFICATION_INSTRUCTION
        assert "1000003-rc" in VERIFICATION_INSTRUCTION


class TestPromptRequiresAtomicQueries:
    """Rule 2. Concrete wrong->right pairs, because abstract wording was measured to fail."""

    def test_there_is_an_explicit_search_atomically_rule(self):
        assert "STEP 2 — SEARCH ATOMICALLY: one bare value per query" in VERIFICATION_INSTRUCTION

    def test_descriptive_phrases_are_forbidden_outright(self):
        assert "**Query the bare value and NOTHING else.**" in VERIFICATION_INSTRUCTION
        assert "never\n   a descriptive phrase" in VERIFICATION_INSTRUCTION

    def test_every_real_failing_query_appears_as_a_worked_wrong_example(self):
        """Abstract guidance already existed and was ignored; these are the actual strings."""
        for prose, _atomic, _body in REAL_MISSES:
            assert prose in VERIFICATION_INSTRUCTION, f"missing worked example for {prose!r}"

    def test_the_right_column_shows_what_is_actually_in_the_evidence(self):
        for fragment in ('`"max":39`', '`"count":195`', "`31.74871794871795`"):
            assert fragment in VERIFICATION_INSTRUCTION

    def test_it_says_matching_is_substring_and_therefore_cannot_match_a_description(self):
        assert "SUBSTRING" in VERIFICATION_INSTRUCTION
        assert "cannot match a description" in VERIFICATION_INSTRUCTION

    def test_it_records_that_the_old_abstract_wording_was_insufficient(self):
        assert "Abstract wording has been measured as insufficient" in VERIFICATION_INSTRUCTION

    def test_units_and_labels_must_be_stripped(self):
        assert "Strip units and labels" in VERIFICATION_INSTRUCTION

    def test_quote_characters_in_queries_are_forbidden(self):
        """Results are json.dumps'd, so `"max":39` cannot match its own text."""
        assert "**Never put quote characters in a query.**" in VERIFICATION_INSTRUCTION


class TestPromptRequiresRetryBeforeRulingAbsent:
    """Rule 3. A miss on a multi-token query is not evidence of absence."""

    def test_there_is_an_explicit_retry_step(self):
        assert "STEP 3 — RETRY WITH THE BARE TOKEN BEFORE RULING ABSENT" in VERIFICATION_INSTRUCTION

    def test_a_multi_token_miss_is_declared_not_to_be_evidence_of_absence(self):
        assert (
            "A `found: false` on a multi-token query is not evidence of absence"
            in VERIFICATION_INSTRUCTION
        )

    def test_only_a_bare_token_miss_supports_a_ruling_of_absence(self):
        assert (
            "Only a `found: false` on a bare, single-token query supports a ruling of absence"
            in VERIFICATION_INSTRUCTION
        )

    def test_the_retry_is_mandatory_not_advisory(self):
        assert "you MUST retry before concluding anything" in VERIFICATION_INSTRUCTION
        assert "The retry is not optional politeness" in VERIFICATION_INSTRUCTION

    def test_the_invalid_ruling_rule_now_requires_the_bare_token_form(self):
        assert (
            "**`found: false` on a bare, single-token query, after the Step 3 retry**"
            in VERIFICATION_INSTRUCTION
        )


class TestNeverSearchedCannotBeValid:
    """Rule 4. This is what let run 5's fabricated revision through with 0 invalid."""

    def test_there_is_an_explicit_rule_forbidding_it(self):
        assert "### A CITATION YOU NEVER SEARCHED CANNOT BE COUNTED VALID" in (
            VERIFICATION_INSTRUCTION
        )

    def test_a_third_unchecked_bucket_exists(self):
        assert "evidence_citations_unchecked" in VERIFICATION_INSTRUCTION

    def test_unchecked_is_neither_valid_nor_invalid(self):
        assert "It is NOT valid. It is NOT invalid either" in VERIFICATION_INSTRUCTION

    def test_the_unchecked_count_is_in_the_output_schema(self):
        assert '"evidence_citations_unchecked": 0,' in VERIFICATION_INSTRUCTION

    def test_validity_by_default_or_omission_is_forbidden(self):
        assert (
            "may not move a citation into `evidence_citations_valid` by default, by omission"
            in VERIFICATION_INSTRUCTION
        )

    def test_unchecked_root_cause_figures_block_medium(self):
        """Direction 2 was a false PASS; the tier must feel it."""
        assert "MEDIUM is not available" in VERIFICATION_INSTRUCTION

    def test_blocking_medium_does_not_open_a_new_route_to_unreliable(self):
        """The tier table is exhaustive — an unchecked citation caps at LOW."""
        assert "This does not create a new route to" in VERIFICATION_INSTRUCTION
        assert "unchecked citations cap the tier at LOW" in VERIFICATION_INSTRUCTION

    def test_the_counts_must_account_for_every_enumerated_citation(self):
        assert "if they do not add up, you skipped something" in VERIFICATION_INSTRUCTION


class TestExistingGuaranteesSurvive:
    """The four behaviours this change must not break. Each is also covered by its own file."""

    def test_the_anti_fabrication_hard_gate_is_intact(self):
        assert "HARD GATE — no evidence means no score" in VERIFICATION_INSTRUCTION
        assert "evidence_unavailable" in VERIFICATION_INSTRUCTION
        assert 'set `diagnosis` = "UNSUPPORTED" and `automated_tier` = "UNRELIABLE"' in (
            VERIFICATION_INSTRUCTION
        )

    def test_a_genuinely_absent_call_is_still_invalid(self):
        assert "no entry for that tool → **INVALID**" in VERIFICATION_INSTRUCTION

    def test_a_complete_result_that_contradicts_a_value_is_still_invalid(self):
        assert "contradicts or\n   omits the value → **INVALID**" in VERIFICATION_INSTRUCTION

    def test_truncated_results_are_still_excluded_from_the_invalid_count(self):
        assert "unverifiable_truncated" in VERIFICATION_INSTRUCTION
        assert "exclude it from `evidence_citations_invalid`" in VERIFICATION_INSTRUCTION

    def test_partial_diagnosis_still_cannot_yield_unreliable(self):
        assert "**A PARTIAL diagnosis can never yield UNRELIABLE.**" in VERIFICATION_INSTRUCTION

    def test_weak_diagnosis_still_yields_low(self):
        assert "**A WEAK diagnosis yields LOW,**" in VERIFICATION_INSTRUCTION

    def test_the_unreliable_routes_are_still_exactly_three(self):
        section = VERIFICATION_INSTRUCTION.split("### The tier table is exhaustive", 1)[1]

        assert "The three rules under UNRELIABLE are the ONLY routes to it" in section

    def test_the_token_budget_still_permits_the_searches_the_rules_mandate(self):
        assert "1-3 tool calls maximum" not in VERIFICATION_INSTRUCTION
        assert "`search_session_evidence` calls" in VERIFICATION_INSTRUCTION

"""Tests for `search_session_evidence` — the retrieval half of ADO-200016.

On the 5-run batch of 2026-08-07, 7 of 8 negative citation rulings were FALSE. The values
(`6901` APM spans, a `184`-point series, `context canceled` at a stated timestamp, four
`200` datapoints) were all present in the raw tool output and absent only from the
truncated view. Two runs were pulled MEDIUM -> LOW by those false invalids.

Truncation cannot fix this. The raw tool output measured 148,884 / 259,261 / 225,917 chars
against a 60,000-char budget, so any pre-cut view shows 23-40% of the evidence; three
different truncation schemes each surfaced the cited value in only 2 of 5 runs. Searching
inverts the economics — the verifier names a value and pays a few hundred chars for the
window around it.

Fixtures below reproduce the real shapes, including run 5's `6901` at offset 8,057 of a
19,752-char result, and the fact that these results are JSON-serialized and contain ZERO
real newlines (see TestNoNewlinesInRealResults) — any line-based processing silently does
nothing and looks like it worked.

The anti-fabrication gate is NOT relaxed: a genuinely absent value must still produce a
zero-match that supports INVALID, and zero retrievable evidence must still produce
`evidence_unavailable`. See TestAbsenceIsRealEvidence and TestHardGateStillIntact.
"""

import json

from app.sub_agents.verification import VERIFICATION_INSTRUCTION, verification_agent
from app.tools.session_tools import (
    _RESULT_HEAD_CHARS,
    _SEARCH_MAX_CHARS,
    _SEARCH_MAX_MATCHES,
    _SEARCH_WINDOW_CHARS,
    _evidence_unavailable,
    _pair_calls_with_results,
    _render_search,
    search_session_evidence,
)


def _call(name, args=None, agent="tier1", call_id=None):
    return {
        "author": agent,
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


def _blob(size, needle="", at=0):
    """A JSON-ish filler string of `size` chars with `needle` planted at offset `at`.

    Uses no newline characters, matching the real serialized results.
    """
    filler = ("span_id=abc123 status=ok duration_ms=42 service=WebCore-webapp " * (size // 60 + 2))
    text = filler[:size]
    if needle:
        text = text[:at] + needle + text[at + len(needle):]
    return text


# Run 5: `6901` sat at offset 8,057 of a 19,752-char fetch_datadog result — roughly nine
# times past the 900-char per-result cap the overview renders.
RUN5_OFFSET = 8057
RUN5_SIZE = 19752
RUN5_VALUE = "6901"


def _run5_session():
    return [
        _call("fetch_datadog", {"query": "apm spans", "from": "2026-08-07T14:00:00Z"}),
        _result(
            "fetch_datadog",
            # The response dict serializes to ~RUN5_SIZE chars with the value well past the cut.
            {"result": _blob(RUN5_SIZE - 60, needle=f"span_count: {RUN5_VALUE},", at=RUN5_OFFSET)},
        ),
    ]


class TestFindsValuesTheTruncatedViewMissed:
    """The five values that were falsely ruled invalid must all be findable."""

    def test_finds_a_value_far_past_the_per_result_truncation_cap(self):
        result = _search(_run5_session(), RUN5_VALUE)

        assert result["found"] is True
        assert result["match_count"] >= 1
        match = result["matches"][0]
        assert match["tool"] == "fetch_datadog"
        assert RUN5_VALUE in match["window"]
        # The whole point: this offset is far beyond what the overview renders.
        assert match["offset"] > _RESULT_HEAD_CHARS

    def test_finds_a_multi_word_log_phrase(self):
        """Run 2's `context canceled` — a phrase, not a number."""
        events = [
            _call("fetch_gcp_logs", {"filter": "severity=ERROR"}),
            _result(
                "fetch_gcp_logs",
                {"result": _blob(7518, needle='msg: "context canceled" ts=23:40:46', at=2020)},
            ),
        ]
        result = _search(events, "context canceled")

        assert result["found"] is True
        assert "context canceled" in result["matches"][0]["window"]

    def test_search_is_case_insensitive(self):
        events = [
            _call("fetch_gcp_logs"),
            _result("fetch_gcp_logs", {"result": _blob(4000, needle="Context Canceled", at=1500)}),
        ]
        assert _search(events, "context canceled")["found"] is True

    def test_counts_every_occurrence_across_results(self):
        """Run 4 cited four `200` datapoints spread over several results."""
        events = []
        for i in range(3):
            events.append(_call("fetch_gcp_metrics", {"seq": i}, call_id=f"m{i}"))
            events.append(
                _result(
                    "fetch_gcp_metrics",
                    {"result": _blob(5000, needle="code:200 code:200", at=1056)},
                    call_id=f"m{i}",
                )
            )
        result = _search(events, "200")

        assert result["found"] is True
        assert result["match_count"] >= 6
        assert result["results_searched"] == 3

    def test_reports_which_tool_and_call_index_produced_the_hit(self):
        events = [
            _call("get_service_context", call_id="c0"),
            _result("get_service_context", {"result": _blob(500)}, call_id="c0"),
            _call("fetch_datadog", {"query": "spans"}, call_id="c1"),
            _result("fetch_datadog", {"result": _blob(3000, needle="6901", at=1200)}, call_id="c1"),
        ]
        match = _search(events, "6901")["matches"][0]

        assert match["tool"] == "fetch_datadog"
        assert match["index"] == 1
        assert match["args"]["query"] == "spans"

    def test_window_carries_surrounding_context_not_just_the_value(self):
        events = [
            _call("fetch_datadog"),
            _result("fetch_datadog", {"result": _blob(9000, needle="6901", at=4000)}),
        ]
        window = _search(events, "6901")["matches"][0]["window"]

        assert len(window) > len("6901")
        assert len(window) <= 2 * _SEARCH_WINDOW_CHARS + len("6901") + 10

    def test_args_travel_with_the_hit_so_the_window_check_can_still_run(self):
        """Check (c) needs the query's arguments, not just the number."""
        events = [
            _call("fetch_gcp_logs", {"from_ts": "2026-08-07T14:00:00Z", "to_ts": "2026-08-07T15:00:00Z"}),
            _result("fetch_gcp_logs", {"result": _blob(6000, needle="1247 errors", at=3000)}),
        ]
        match = _search(events, "1247")["matches"][0]

        assert match["args"]["from_ts"] == "2026-08-07T14:00:00Z"
        assert match["args"]["to_ts"] == "2026-08-07T15:00:00Z"


class TestAbsenceIsRealEvidence:
    """A zero-match is the anti-fabrication half — it must stay unambiguous."""

    def test_absent_value_returns_a_clean_zero(self):
        events = [
            _call("fetch_datadog"),
            _result("fetch_datadog", {"result": _blob(9000)}),
        ]
        result = _search(events, "99999-never-emitted")

        assert result["found"] is False
        assert result["match_count"] == 0
        assert result["matches"] == []

    def test_zero_match_states_how_many_results_were_searched(self):
        events = []
        for i in range(4):
            events.append(_call("fetch_datadog", {"seq": i}, call_id=f"c{i}"))
            events.append(_result("fetch_datadog", {"result": _blob(2000)}, call_id=f"c{i}"))
        result = _search(events, "absent-value-xyz")

        assert result["results_searched"] == 4
        assert "all 4 tool results" in result["instruction_to_verifier"]

    def test_zero_match_tells_the_verifier_this_supports_invalid(self):
        """'Searched N results, not found' must read as absence, not as a coverage gap."""
        events = [_call("fetch_datadog"), _result("fetch_datadog", {"result": _blob(3000)})]
        instruction = _search(events, "absent-value-xyz")["instruction_to_verifier"]

        assert "evidence of absence" in instruction
        assert "INVALID" in instruction
        assert "not a truncation blind spot" in instruction

    def test_a_found_value_tells_the_verifier_not_to_rule_invalid(self):
        events = [
            _call("fetch_datadog"),
            _result("fetch_datadog", {"result": _blob(9000, needle="6901", at=4000)}),
        ]
        instruction = _search(events, "6901")["instruction_to_verifier"]

        assert "WAS found" in instruction
        assert "do NOT rule it invalid for absence" in instruction
        # Finding the number must not excuse the window check.
        assert "check (c)" in instruction


class TestResponseIsBounded:
    """A pathological query must not blow the prompt."""

    def test_a_query_matching_everything_stays_within_the_cap(self):
        events = []
        for i in range(20):
            events.append(_call("fetch_datadog", {"seq": i}, call_id=f"c{i}"))
            events.append(_result("fetch_datadog", {"result": _blob(20000)}, call_id=f"c{i}"))
        raw = _render_search("e", _pair_calls_with_results(events))

        assert len(raw) < _SEARCH_MAX_CHARS * 2
        assert json.loads(raw)["search_capped"] is True

    def test_capped_response_still_reports_the_true_match_count(self):
        events = []
        for i in range(20):
            events.append(_call("fetch_datadog", {"seq": i}, call_id=f"c{i}"))
            events.append(_result("fetch_datadog", {"result": _blob(20000)}, call_id=f"c{i}"))
        result = _search(events, "e")

        assert result["matches_shown"] <= _SEARCH_MAX_MATCHES
        # Counting continues past the window budget, so the total is the real total.
        assert result["match_count"] > result["matches_shown"] * 10
        assert str(result["match_count"]) in result["instruction_to_verifier"]

    def test_one_huge_blob_cannot_consume_every_match_slot(self):
        events = [
            _call("fetch_datadog", call_id="big"),
            _result("fetch_datadog", {"result": "6901 " * 5000}, call_id="big"),
            _call("fetch_gcp_logs", call_id="small"),
            _result("fetch_gcp_logs", {"result": _blob(3000, needle="6901", at=1500)}, call_id="small"),
        ]
        result = _search(events, "6901")

        assert {m["tool"] for m in result["matches"]} == {"fetch_datadog", "fetch_gcp_logs"}

    def test_a_too_short_query_is_rejected_rather_than_matching_everything(self):
        result = json.loads(search_session_evidence(tool_context=None, query="e"))

        assert result["search_failed"] is True
        assert "at least" in result["error"]

    def test_diagnostic_results_get_window_slots_before_the_preamble(self):
        """Run 4's `200` also appears in load_infra_context; the metrics call matters more."""
        events = [
            _call("load_infra_context", call_id="p0"),
            _result("load_infra_context", {"result": "200 " * 400}, call_id="p0"),
            _call("fetch_gcp_metrics", call_id="d0"),
            _result("fetch_gcp_metrics", {"result": _blob(4000, needle="200", at=1056)}, call_id="d0"),
        ]
        result = _search(events, "200")
        tools = [m["tool"] for m in result["matches"]]

        assert "fetch_gcp_metrics" in tools


class TestSelfReferentialResultsAreExcluded:
    """A value must be confirmed by the tool that produced it, not by the verifier's echo.

    Run 4 of the real batch contains the cited `200` inside a recorded
    get_session_tool_outputs result — searching that would confirm a citation from a prior
    rendering of the evidence rather than from a diagnostic tool. Circular.
    """

    def test_prior_evidence_retrievals_are_not_searched(self):
        events = [
            _call("get_session_tool_outputs", call_id="s0"),
            _result("get_session_tool_outputs", {"result": _blob(9000, needle="6901", at=4000)},
                    call_id="s0"),
        ]
        result = _search(events, "6901")

        assert result["found"] is False
        assert result["results_searched"] == 0

    def test_a_real_tool_still_confirms_the_same_value(self):
        events = [
            _call("get_session_tool_outputs", call_id="s0"),
            _result("get_session_tool_outputs", {"result": _blob(9000, needle="6901", at=4000)},
                    call_id="s0"),
            _call("fetch_datadog", call_id="d0"),
            _result("fetch_datadog", {"result": _blob(9000, needle="6901", at=4000)}, call_id="d0"),
        ]
        result = _search(events, "6901")

        assert result["found"] is True
        assert {m["tool"] for m in result["matches"]} == {"fetch_datadog"}


class TestNoNewlinesInRealResults:
    """The trap: these payloads are JSON-serialized and contain ZERO real newlines.

    Run 5's 19,752-char result has none. Line-based processing silently does nothing
    while appearing to work, which cost a previous attempt an entire iteration.
    """

    def test_the_fixture_really_has_no_newlines(self):
        pairs = _pair_calls_with_results(_run5_session())
        result_text = pairs[0]["result"]

        assert "\n" not in result_text
        assert len(result_text.splitlines()) == 1

    def test_search_works_on_a_single_line_payload(self):
        pairs = _pair_calls_with_results(_run5_session())
        assert "\n" not in pairs[0]["result"]

        assert _search(_run5_session(), RUN5_VALUE)["found"] is True


class TestHardGateStillIntact:
    """The anti-fabrication guarantees. These must never be relaxed."""

    def test_a_session_with_no_tool_calls_is_evidence_unavailable_not_zero_match(self):
        """'No evidence' and 'searched and absent' are different verdicts."""
        events = [{"author": "root", "content": {"parts": [{"text": "no tools were used"}]}}]
        result = json.loads(_render_search("6901", _pair_calls_with_results(events)))

        assert result["evidence_unavailable"] is True
        assert "UNRELIABLE" in result["instruction_to_verifier"]
        assert "found" not in result

    def test_unavailable_evidence_never_reads_as_a_confirmed_absence(self):
        unavailable = json.loads(_evidence_unavailable("transport failed"))

        assert unavailable["evidence_unavailable"] is True
        assert unavailable["instruction_to_verifier"].startswith(
            "Raw tool evidence could NOT be retrieved"
        )

    def test_results_never_recorded_are_not_counted_as_searched(self):
        """A call with no result is not evidence that was examined."""
        events = [_call("fetch_datadog")]
        result = json.loads(_render_search("6901", _pair_calls_with_results(events)))

        assert result["found"] is False
        assert result["results_searched"] == 0


class TestPromptWiring:
    def test_the_search_tool_is_registered_on_the_verification_agent(self):
        names = {getattr(t, "__name__", getattr(t, "name", "")) for t in verification_agent.tools}

        assert "search_session_evidence" in names
        # Additive: the overview must remain.
        assert "get_session_tool_outputs" in names

    def test_prompt_requires_a_search_before_ruling_invalid_for_absence(self):
        assert "MANDATORY: search before you rule INVALID for absence" in VERIFICATION_INSTRUCTION
        assert "until you have" in VERIFICATION_INSTRUCTION

    def test_prompt_tells_the_verifier_a_zero_match_supports_invalid(self):
        assert "SUPPORTS an INVALID ruling" in VERIFICATION_INSTRUCTION

    def test_prompt_tells_the_verifier_a_hit_clears_check_b_but_not_check_c(self):
        assert "PASSES check (b)" in VERIFICATION_INSTRUCTION
        assert "then apply\n  check (c)" in VERIFICATION_INSTRUCTION

    def test_prompt_budget_permits_the_searches_it_mandates(self):
        """The old budget said '1-3 tool calls maximum', which forbade searching."""
        assert "1-3 tool calls maximum" not in VERIFICATION_INSTRUCTION
        assert "search_session_evidence" in VERIFICATION_INSTRUCTION

    def test_truncated_results_now_route_to_the_search(self):
        assert "**SEARCH IT**" in VERIFICATION_INSTRUCTION

    def test_the_hard_gate_language_survives_this_change(self):
        assert "HARD GATE — no evidence means no score" in VERIFICATION_INSTRUCTION
        assert 'set `diagnosis` = "UNSUPPORTED" and `automated_tier` = "UNRELIABLE"' in (
            VERIFICATION_INSTRUCTION
        )

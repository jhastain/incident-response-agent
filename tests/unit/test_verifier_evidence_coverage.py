"""Regression tests for the verifier-truncation defect measured on 2026-07-29.

Five live runs were all tiered UNRELIABLE. The tools had run, the numbers were real,
and 0 results were malformed — but `get_session_tool_outputs` returned only the first
25 *list items* of 37-63, chronologically. Because calls and results were separate
items, the infra_context/doc-loading preamble every run starts with consumed the budget
and the diagnostic RESULTS (fetch_gcp_logs, fetch_datadog, fetch_gcp_metrics,
fetch_sentry, ...) fell off the end. The verifier then reported, correctly, that it
could not verify — and the hard gate turned that into UNRELIABLE.

Two of those runs (T1, T6) came back PARTIAL diagnosis + UNRELIABLE tier, which no rule
in the tier table permits. See TestTierTableConsistency.

The anti-fabrication gate itself is NOT relaxed: a session with genuinely zero
retrievable evidence must still produce `evidence_unavailable`. See TestHardGateIntact.
"""

import json

from app.sub_agents.verification import VERIFICATION_INSTRUCTION
from app.tools.session_tools import (
    _ARG_VALUE_CHARS,
    _MAX_EVIDENCE_CHARS,
    _build_payload,
    _evidence_unavailable,
    _pair_calls_with_results,
    _select_within_budget,
    _truncate_dict,
)

# Tools whose results the report actually cites as evidence for a root cause.
DIAGNOSTIC_TOOLS = {
    "fetch_gcp_logs",
    "fetch_gcp_metrics",
    "fetch_datadog",
    "fetch_sentry",
    "fetch_cloud_run",
    "get_deploy_task_detail",
    "find_similar_incidents",
}

# The preamble every observed run opens with, which used to eat the whole budget.
PREAMBLE_TOOLS = [
    "get_service_context",
    "load_infra_context",
    "load_infra_context",
    "load_infra_context",
    "listToolFiles",
    "list_incident_files",
    "read_incident_file",
    "get_triage_guidance",
]


def _call(name, args=None, agent="tier1"):
    return {
        "author": agent,
        "content": {"parts": [{"functionCall": {"id": name, "name": name, "args": args or {}}}]},
    }


def _result(name, payload):
    return {
        "author": name,
        "content": {
            "parts": [{"functionResponse": {"id": name, "name": name, "response": payload}}]
        },
    }


def _session(tool_names, result_size=200):
    """A session shaped like the real ones: preamble first, diagnostics after."""
    events = []
    for i, name in enumerate(tool_names):
        events.append(_call(name, {"seq": i}))
        events.append(_result(name, {"data": f"{name}-value-{i}-" + "x" * result_size}))
    return events


def _payload(events):
    pairs = _pair_calls_with_results(events)
    selected, omitted = _select_within_budget(pairs)
    return _build_payload("sess-1", len(events), pairs, selected, omitted)


class TestDiagnosticEvidenceSurvives:
    """The defect itself: diagnostic results must not be crowded out by the preamble."""

    def test_preamble_does_not_starve_diagnostic_results(self):
        events = _session(PREAMBLE_TOOLS + sorted(DIAGNOSTIC_TOOLS))
        payload = _payload(events)

        shown = {e["tool"] for e in payload["tool_calls"] if e["result_summary"]}
        missing = DIAGNOSTIC_TOOLS - shown
        assert not missing, f"diagnostic results dropped from evidence: {sorted(missing)}"

    def test_result_is_attached_to_the_call_that_produced_it(self):
        """Check (c) compares a citation's window to the query's arguments.

        The old shape emitted calls and results as independent list items, so the
        arguments and the numbers they produced could land on opposite sides of the
        cut. They must travel together.
        """
        events = [
            _call("fetch_gcp_logs", {"from_ts": "2026-07-29T00:00:00Z", "to_ts": "2026-07-29T04:00:00Z"}),
            _result("fetch_gcp_logs", {"entries": 1247}),
        ]
        entry = _payload(events)["tool_calls"][0]

        assert entry["tool"] == "fetch_gcp_logs"
        assert entry["args"]["from_ts"] == "2026-07-29T00:00:00Z"
        assert "1247" in entry["result_summary"]

    def test_a_60_call_session_stays_within_the_prompt_budget(self):
        events = _session(sorted(DIAGNOSTIC_TOOLS) * 9, result_size=4000)
        payload = _payload(events)

        assert len(json.dumps(payload)) <= _MAX_EVIDENCE_CHARS * 1.2

    def test_time_parameters_survive_a_long_argument(self):
        """A datadog call in run T6 carried from=/to= past the old 200-char arg cut."""
        code = (
            "result = datadog_token.search_datadog_logs(\n"
            f"    query=\"{'IBD unhealthy ' * 20}\",\n"
            '    from="2026-07-29T00:00:00Z",\n'
            '    to="2026-07-29T04:00:00Z",\n)'
        )
        assert len(code) > _ARG_VALUE_CHARS  # the cut really does bite here
        rendered = json.dumps(_truncate_dict({"code": code}))

        assert "2026-07-29T00:00:00Z" in rendered
        assert "2026-07-29T04:00:00Z" in rendered


class TestHardGateIntact:
    """The anti-fabrication guarantee. These must never be relaxed."""

    def test_session_with_no_tool_calls_is_evidence_unavailable(self):
        events = [{"author": "root", "content": {"parts": [{"text": "no tools were used"}]}}]
        result = json.loads(
            _evidence_unavailable("the session contains no tool calls at all")
        )

        assert result["evidence_unavailable"] is True
        assert "UNRELIABLE" in result["instruction_to_verifier"]
        assert not _pair_calls_with_results(events)

    def test_unavailable_and_partial_are_distinct_signals(self):
        """Conflating these is the bug: it grades a sound run as a fabricated one."""
        partial = _payload(_session(sorted(DIAGNOSTIC_TOOLS) * 12, result_size=4000))

        assert partial.get("evidence_partial") is True
        assert "evidence_unavailable" not in partial
        assert partial["tool_call_count"] > 0

    def test_partial_payload_tells_the_verifier_not_to_tier_unreliable(self):
        payload = _payload(_session(sorted(DIAGNOSTIC_TOOLS) * 12, result_size=4000))
        instruction = payload["instruction_to_verifier"]

        assert "NOT `evidence_unavailable`" in instruction
        assert "do NOT set automated_tier=UNRELIABLE" in instruction


class TestCoverageIsReported:
    def test_complete_retrieval_reports_full_coverage_and_no_partial_flag(self):
        payload = _payload(_session(sorted(DIAGNOSTIC_TOOLS)))

        assert payload["evidence_coverage"] == "7 of 7 tool calls shown in full"
        assert "evidence_partial" not in payload

    def test_bounded_retrieval_reports_what_was_omitted(self):
        payload = _payload(_session(sorted(DIAGNOSTIC_TOOLS) * 12, result_size=4000))

        shown = payload["tool_calls_shown"]
        total = payload["tool_call_count"]
        assert payload["evidence_coverage"] == f"{shown} of {total} tool calls shown in full"
        assert shown < total
        assert payload["omitted_tools"]

    def test_results_are_counted_so_the_verifier_can_see_evidence_exists(self):
        payload = _payload(_session(sorted(DIAGNOSTIC_TOOLS)))

        assert payload["tool_results_available"] == 7


class TestTierTableConsistency:
    """Runs T1 and T6 returned PARTIAL diagnosis with UNRELIABLE tier.

    The tier table admits three routes to UNRELIABLE, and none of them is reachable
    from a PARTIAL diagnosis. The prompt now says so explicitly.
    """

    def test_prompt_forbids_partial_diagnosis_from_yielding_unreliable(self):
        assert "A PARTIAL diagnosis can never yield UNRELIABLE" in VERIFICATION_INSTRUCTION

    def test_prompt_scopes_the_evidence_trigger_to_total_absence(self):
        assert "could not be retrieved AT ALL" in VERIFICATION_INSTRUCTION

    def test_prompt_tells_the_verifier_to_state_coverage(self):
        assert "checked N of M tool calls" in VERIFICATION_INSTRUCTION

    def test_prompt_separates_partial_coverage_from_the_hard_gate(self):
        assert "PARTIAL COVERAGE IS NOT THE HARD GATE" in VERIFICATION_INSTRUCTION
        assert "It does NOT fire on\n`evidence_partial: true`" in VERIFICATION_INSTRUCTION

    def test_prompt_still_fires_the_gate_on_genuine_absence(self):
        assert "HARD GATE — no evidence means no score" in VERIFICATION_INSTRUCTION
        assert 'set `diagnosis` = "UNSUPPORTED" and `automated_tier` = "UNRELIABLE"' in (
            VERIFICATION_INSTRUCTION
        )

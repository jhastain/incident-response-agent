"""Datadog time-window precedence (ADO-200004, defect #1).

Precedence for a time-scoped Datadog call:
  1. explicit window provided        -> use it, no warning
  2. no window + alert timestamp     -> label DERIVED, name the incident bounds
  3. neither                         -> last-hour default, labelled as such

Graceful default in every branch: the call always returns data. It is never
rejected. What changes is how honestly the window is labelled, because the
verifier scores citations on whether a figure really came from the incident
window (5-run e2e 2026-07-28: 22 unconstrained warnings, citation validity 21%).
"""

from __future__ import annotations

import json

from app.sub_agents.tier1.datadog_tierb import (
    _TIME_SCOPED_TOOLS,
    _WINDOWABLE_TOOLS,
    _alert_timestamp,
    _dropped_kwarg_calls,
    _ineffective_range_calls,
    _reissue_form,
    _sql_only_window_calls,
    _unconstrained_time_calls,
    _window_label,
)

ALERT_TS = "2026-07-27T13:28:00Z"

# The three real offending calls captured from the 2026-07-28 live runs.
REAL_OFFENDERS = [
    "result = datadog_token.search_datadog_events(query='source:hangfire service:acme.example')",
    "result = datadog_token.search_datadog_logs(query='service:hangfire-recurring error')",
    "result = datadog_token.search_datadog_events(query='source:datadog type:alert hangfire')",
]


class _State:
    def __init__(self, data):
        self._d = data

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Ctx:
    def __init__(self, data):
        self.state = _State(data)


class TestDetection:
    def test_real_failing_calls_are_flagged(self):
        for code in REAL_OFFENDERS:
            assert _unconstrained_time_calls(code), f"missed offender: {code}"

    def test_acme_net_is_not_a_false_positive(self):
        """'service:acme.example' contains 'end' - an earlier detector matched the
        bare substring and let the offending call through as if constrained."""
        code = REAL_OFFENDERS[0]
        assert "acme.example" in code
        assert _unconstrained_time_calls(code) == ["search_datadog_events"]

    def test_from_to_dict_keys_are_accepted(self):
        """The real call form. `from` is a reserved word, so it must be dict-unpacked."""
        code = (
            'args = {"query": "service:x", "from": "2026-07-27T13:00:00Z", '
            '"to": "2026-07-27T14:00:00Z"}\n'
            "result = datadog_token.search_datadog_logs(**args)"
        )
        assert _unconstrained_time_calls(code) == []

    def test_literal_to_kwarg_is_accepted(self):
        """Only `from` is reserved; `to=` can be written literally."""
        code = "result = datadog_token.search_datadog_spans(query='service:x', to='now')"
        assert _unconstrained_time_calls(code) == []

    def test_start_end_is_rejected_because_the_server_drops_it(self):
        """start=/end= is in NO Datadog tool signature; the span tools accept and ignore it.

        Live-measured 2026-07-28, one fixed aggregate_spans query, sequential calls:
            no window 514000 | start=/end= 513995 | from/to 467682 | no window 514094
        start=/end= tracked the rolling default. Counting it as a constraint would
        suppress the warning on a call that silently returned last-hour data.
        """
        code = (
            "result = datadog_token.aggregate_spans(query='service:x', "
            "start='2026-07-27T13:00:00Z', end='2026-07-27T14:00:00Z')"
        )
        assert _unconstrained_time_calls(code) == ["aggregate_spans"]

    def test_start_at_is_pagination_not_a_time_bound(self):
        """`start_at` is a paging cursor on these tools. It must not read as a window."""
        code = "result = datadog_token.search_datadog_logs(query='service:x', start_at=100)"
        assert _unconstrained_time_calls(code) == ["search_datadog_logs"]

    def test_in_query_relative_bound_is_not_a_constraint(self):
        """Live-measured: query="service:acme.example now-1h" -> total_buckets 0.

        The server treats it as a search term, not a bound. Passed properly as
        {"from": "now-1h", "to": "now"} the same query returned 434,696.
        """
        code = "result = datadog_token.search_datadog_logs(query='service:x now-2h')"
        assert _unconstrained_time_calls(code) == ["search_datadog_logs"]

    def test_in_query_timestamp_range_is_still_rejected(self):
        """The MCP server drops @timestamp:[...] - verified 2026-07-27. Accepting it
        would suppress the warning while the query silently ran against now."""
        code = (
            "result = datadog_token.search_datadog_logs("
            "query='@timestamp:[2026-07-27T13:00:00Z TO 2026-07-27T14:00:00Z] service:x')"
        )
        assert _unconstrained_time_calls(code) == ["search_datadog_logs"]
        assert _ineffective_range_calls(code) == ["search_datadog_logs"]

    def test_non_time_scoped_tool_not_flagged(self):
        code = "result = datadog_token.get_datadog_dashboard(dashboard_id='aaa-111-bbb')"
        assert _unconstrained_time_calls(code) == []


class TestAlertTimestampResolution:
    def test_reads_dict_classification(self):
        ctx = _Ctx({"classification": {"alert_timestamp": ALERT_TS}})
        assert _alert_timestamp(ctx) == ALERT_TS

    def test_reads_json_string_classification(self):
        ctx = _Ctx({"classification": json.dumps({"alert_timestamp": ALERT_TS})})
        assert _alert_timestamp(ctx) == ALERT_TS

    def test_reads_fenced_json_classification(self):
        raw = "```json\n" + json.dumps({"alert_timestamp": ALERT_TS}) + "\n```"
        assert _alert_timestamp(_Ctx({"classification": raw})) == ALERT_TS

    def test_unknown_timestamp_is_absent(self):
        """A5 (raw Datadog monitor payload, no epoch) really does emit "unknown"."""
        for val in ("unknown", "Unknown", "", "none", "null"):
            ctx = _Ctx({"classification": {"alert_timestamp": val}})
            assert _alert_timestamp(ctx) == "", f"{val!r} should resolve to absent"

    def test_missing_state_is_absent_not_an_error(self):
        assert _alert_timestamp(None) == ""
        assert _alert_timestamp(_Ctx({})) == ""

    def test_malformed_classification_is_absent_not_an_error(self):
        assert _alert_timestamp(_Ctx({"classification": "not json at all"})) == ""
        assert _alert_timestamp(_Ctx({"classification": 42})) == ""


class TestPrecedenceLabelling:
    """Precedence for a tool that CAN carry a window (aggregate_spans)."""

    def test_case2_derived_names_the_incident_bounds(self):
        label = _window_label(["aggregate_spans"], ALERT_TS)
        assert "WINDOW DERIVED FROM ALERT TIMESTAMP" in label
        assert ALERT_TS in label
        assert "30min" in label and "15min" in label

    def test_case3_default_says_last_hour(self):
        label = _window_label(["aggregate_spans"], "")
        assert "LAST-HOUR DEFAULT" in label
        assert "alert timestamp is unknown" in label

    def test_both_branches_forbid_citing_as_incident_evidence(self):
        """Whichever branch fires, the result must never read as incident-window data."""
        for tool in ("aggregate_spans", "search_datadog_logs"):
            for ts in (ALERT_TS, ""):
                label = _window_label([tool], ts).lower()
                assert "not the incident window" in label or "not incident-window" in label

    def test_label_names_the_offending_tool(self):
        label = _window_label(["search_datadog_events", "search_datadog_logs"], "")
        assert "search_datadog_events" in label and "search_datadog_logs" in label

    def test_neither_branch_rejects_the_call(self):
        """Graceful default: the guard only labels. It never raises or refuses."""
        for tool in ("aggregate_spans", "search_datadog_logs"):
            for ts in (ALERT_TS, ""):
                assert isinstance(_window_label([tool], ts), str)


class TestEveryTimeScopedToolIsWindowable:
    """EVERY time-scoped Datadog tool takes `from`/`to`. There is no unfixable class.

    EVIDENCE TIER: LIVE-VERIFIED 2026-07-28 from the AUTHORITATIVE schema
    (getToolDocs(server="datadog_token", tool=...)), not from guessed parameter names.
    Every signature below declares `from` and `to`; NONE declares start/end/from_ts/to_ts:

        analyze_datadog_logs(sql_query, telemetry, extra_columns, filter, from, indexes,
                             max_tokens, start_at, storage_tier, to)
        search_datadog_logs(query, telemetry, ..., from, ..., to, use_log_patterns)
        search_datadog_events(telemetry, from, max_tokens, query, sort, start_at, to)
        get_datadog_metric(queries, telemetry, formulas, from, interval, ..., to, ...)
        aggregate_spans(computes, query, telemetry, from, group_by, max_tokens,
                        start_at, to)
        search_datadog_spans(query, telemetry, custom_attributes, from, max_tokens,
                             sort, start_at, to)
        search_datadog_error_tracking_issues(telemetry, assignee, from, max_tokens,
                                             order_by, query, states, team, to)

    The previous "these four REJECT every time argument" finding was a MEASUREMENT
    ARTEFACT: `from` is a reserved word, so it can only be passed by dict-unpacking, and
    the earlier probes never tried that — they tried start/end/from_ts/to_ts, got
    rejections, and concluded the tools were non-windowable. Four tools' worth of
    incident-window evidence was discarded on the strength of a wrong kwarg name.
    """

    ALL_TIME_SCOPED = [
        "aggregate_spans",
        "search_datadog_spans",
        "search_datadog_error_tracking_issues",
        "analyze_datadog_logs",
        "search_datadog_logs",
        "search_datadog_events",
        "get_datadog_metric",
    ]

    def test_every_time_scoped_tool_is_in_the_windowable_set(self):
        assert _WINDOWABLE_TOOLS == frozenset(self.ALL_TIME_SCOPED)

    def test_the_list_matches_the_declared_pyi_signatures(self):
        """Ground truth is servers/datadog_token.pyi, not probe responses.

        Read live 2026-07-28 via readToolFile(fileName="servers/datadog_token.pyi").
        Every tool below declares `from: str = None, to: str = None`; NONE declares
        `start` or `end`. search_datadog_monitors declares no window param at all and is
        therefore correctly ABSENT from the windowable set:

            aggregate_spans                       from=True  to=True  start=False end=False
            search_datadog_spans                  from=True  to=True  start=False end=False
            search_datadog_error_tracking_issues  from=True  to=True  start=False end=False
            search_datadog_logs                   from=True  to=True  start=False end=False
            analyze_datadog_logs                  from=True  to=True  start=False end=False
            search_datadog_events                 from=True  to=True  start=False end=False
            get_datadog_metric                    from=True  to=True  start=False end=False
            search_datadog_monitors               from=False to=False start=False end=False
        """
        # search_datadog_monitors is the ONLY Datadog tool with no window parameter.
        assert "search_datadog_monitors" not in _WINDOWABLE_TOOLS
        # Every tool the guard treats as time-scoped must be windowable, and vice versa:
        # a time-scoped tool missing from the windowable set is one whose window the
        # agent would never be told to pass.
        assert _WINDOWABLE_TOOLS == frozenset(_TIME_SCOPED_TOOLS)
        assert len(_WINDOWABLE_TOOLS) == 7

    def test_every_tool_gets_the_reissue_path(self):
        for tool in self.ALL_TIME_SCOPED:
            label = _window_label([tool], ALERT_TS)
            assert "WINDOW DERIVED FROM ALERT TIMESTAMP" in label, tool
            assert "NOT FIXABLE" not in label, tool

    def test_no_label_ever_says_not_fixable(self):
        """The retired branch. Every tool can carry the window, so nothing is unfixable."""
        for tool in self.ALL_TIME_SCOPED:
            for ts in (ALERT_TS, ""):
                assert "NOT FIXABLE" not in _window_label([tool], ts), tool

    def test_analyze_datadog_logs_is_windowable(self):
        """Corrects the prior 'MEASURED non-windowable' claim, which was wrong.

        Live-verified: {"sql_query": "SELECT min(timestamp), max(timestamp) FROM logs",
        "from": "2026-07-27T13:00:00Z", "to": "2026-07-27T14:00:00Z"} returned
        mn 13:00:00.005Z / mx 13:59:59.995Z — exactly the requested window.
        """
        assert "analyze_datadog_logs" in _WINDOWABLE_TOOLS
        label = _window_label(["analyze_datadog_logs"], ALERT_TS)
        assert "WINDOW DERIVED FROM ALERT TIMESTAMP" in label
        assert "NOT FIXABLE" not in label

    def test_get_datadog_metric_is_windowable(self):
        """Also corrected: it takes queries= (a LIST) AND from/to.

        Live-verified: {"queries": ["avg:system.cpu.user{*}"], "from": ..., "to": ...}
        returned time_range ["2026-07-27T13:00:00Z", "2026-07-27T13:59:40Z"].
        """
        assert "get_datadog_metric" in _WINDOWABLE_TOOLS
        label = _window_label(["get_datadog_metric"], ALERT_TS)
        assert "WINDOW DERIVED FROM ALERT TIMESTAMP" in label

    def test_reissue_advice_uses_dict_unpacking_never_a_bare_from_kwarg(self):
        """`from=` is a SyntaxError in Starlark. The suggested CALL must never use it.

        The surrounding prose may mention `from=` in order to forbid it; what must not
        happen is the executable form containing it.
        """
        for tool in self.ALL_TIME_SCOPED:
            form = _reissue_form(tool, ALERT_TS)
            assert '"from"' in form and '"to"' in form, tool
            assert f"{tool}(**args)" in form, tool
            assert "from=" not in form, tool
            assert "start=" not in form and "end=" not in form, tool

            label = _window_label([tool], ALERT_TS)
            assert form in label, tool

    def test_reissue_advice_names_the_right_first_parameter(self):
        """sql_query for analyze_datadog_logs, queries for get_datadog_metric."""
        assert '"sql_query"' in _window_label(["analyze_datadog_logs"], ALERT_TS)
        assert '"queries"' in _window_label(["get_datadog_metric"], ALERT_TS)
        assert '"query"' in _window_label(["aggregate_spans"], ALERT_TS)

    def test_labels_warn_against_the_silently_dropped_kwargs(self):
        label = _window_label(["aggregate_spans"], ALERT_TS)
        assert "start=" in label and "silently DROPPED" in label

    def test_windowable_tool_is_told_to_reissue(self):
        label = _window_label(["aggregate_spans"], ALERT_TS)
        assert "re-issue" in label.lower()
        assert "NOT FIXABLE" not in label

    def test_prompts_offer_every_windowed_tool(self):
        """Expanding _WINDOWABLE_TOOLS only changes labelling after the fact.

        If the prompt never suggests these calls, the recovered coverage is theoretical.
        """
        from app.sub_agents.tier1.datadog_observability import (
            INSTRUCTION as OBS_INSTRUCTION,
        )
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as TIERB_INSTRUCTION

        for tool in self.ALL_TIME_SCOPED:
            assert tool in TIERB_INSTRUCTION, f"tierb prompt omits {tool}"
            assert tool in OBS_INSTRUCTION, f"observability prompt omits {tool}"

    def test_prompts_flag_truncated_span_counts_as_lower_bounds(self):
        """search_datadog_spans returns is_truncated=true; a subset is not a total."""
        from app.sub_agents.tier1.datadog_observability import (
            INSTRUCTION as OBS_INSTRUCTION,
        )
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as TIERB_INSTRUCTION

        for instruction in (TIERB_INSTRUCTION, OBS_INSTRUCTION):
            assert "truncated" in instruction.lower()
            assert "at least N (truncated)" in instruction

    def test_known_alert_time_now_triggers_a_retry_that_can_succeed(self):
        """Inverts the old expectation: the retry is no longer doomed.

        search_datadog_logs takes from/to, so deriving bounds from the incident time
        produces a call that really returns incident-window data.
        """
        label = _window_label(["search_datadog_logs"], ALERT_TS)
        assert "WINDOW DERIVED FROM ALERT TIMESTAMP" in label
        assert "CANNOT be applied" not in label
        assert ALERT_TS in label

    def test_mixed_batch_gives_every_tool_the_reissue_form(self):
        label = _window_label(["aggregate_spans", "search_datadog_logs"], ALERT_TS)
        assert "WINDOW DERIVED FROM ALERT TIMESTAMP" in label
        assert "NOT FIXABLE" not in label
        assert "aggregate_spans(**args)" in label
        assert "search_datadog_logs(**args)" in label

    def test_the_real_offending_calls_are_now_labelled_reissuable(self):
        """The three calls captured from the live runs. All are fixable after all."""
        for code in REAL_OFFENDERS:
            offenders = _unconstrained_time_calls(code)
            assert offenders, code
            label = _window_label(offenders, ALERT_TS)
            assert "WINDOW DERIVED FROM ALERT TIMESTAMP" in label
            assert "NOT FIXABLE" not in label

    def test_label_still_forbids_incident_citation(self):
        """Rewriting the advice must NOT weaken the anti-fabrication rule."""
        label = _window_label(["search_datadog_logs"], "")
        assert "NOT the incident window" in label
        assert "not incident-window" in label.lower()

    def test_every_branch_keeps_the_marker_synthesis_greps_for(self):
        """synthesis.py refuses any finding carrying this exact string as evidence.

        The DERIVED branch must keep it too: the result AS RETURNED is still last-hour
        data, so losing the marker would let a now-scoped number through as incident
        evidence — the exact hole this guard exists to close.
        """
        marker = "!! UNCONSTRAINED TIME WINDOW"
        for tool in self.ALL_TIME_SCOPED:
            for ts in (ALERT_TS, ""):
                assert marker in _window_label([tool], ts), f"{tool} ({ts!r}) lost the marker"


class TestPromptsMatchTheVerifiedSchema:
    """The prompts previously mandated arguments no tool declares.

    Worse than the 108 "unexpected additional properties" rejections in the traces: on
    the span tools the mandated `start=`/`end=` was ACCEPTED and silently dropped, so
    the model followed the instructions, got last-hour data, and reported it as
    incident-window evidence.
    """

    def test_tierb_prompt_teaches_dict_unpacked_from_to(self):
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION

        assert "<from_param>" not in INSTRUCTION
        assert '"from"' in INSTRUCTION and '"to"' in INSTRUCTION
        assert "**args" in INSTRUCTION
        assert "reserved word" in INSTRUCTION.lower()

    def test_tierb_prompt_forbids_the_silently_dropped_kwargs(self):
        """The prompt used to MANDATE start=/end= on the three span tools."""
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION

        assert "NEVER use `start=`" in INSTRUCTION
        assert 'aggregate_spans(query="<filters>", start=' not in INSTRUCTION
        assert 'search_datadog_spans(query="<filters>", start=' not in INSTRUCTION

    def test_tierb_prompt_no_longer_claims_tools_reject_windows(self):
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION

        assert "NOT FIXABLE" not in INSTRUCTION
        assert "REJECT every time argument" not in INSTRUCTION
        assert "Window NOT SUPPORTED" not in INSTRUCTION

    def test_tierb_prompt_calls_get_datadog_metric_correctly(self):
        """It was never broken — the agent was passing the wrong parameter name.

        Live-verified 2026-07-28: `query=` -> "unexpected additional properties";
        omitting it -> 'required: missing properties: ["queries"]'; `queries=[...]`
        SUCCEEDS. One wrong kwarg explains every historical failure, so the fix is to
        call it correctly, not to drop the tool.
        """
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION

        assert '"queries"' in INSTRUCTION
        assert "AVOID `get_datadog_metric`" not in INSTRUCTION

    def test_tierb_prompt_treats_no_data_and_modifiers_as_findings(self):
        """A NO_DATA_WARNING rules a hypothesis out; it is evidence, not a failure."""
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION

        assert "NO_DATA_WARNING" in INSTRUCTION
        assert "AUTO_MODIFIERS" in INSTRUCTION

    def test_observability_prompt_calls_get_datadog_metric_correctly(self):
        from app.sub_agents.tier1.datadog_observability import INSTRUCTION

        assert "queries=" in INSTRUCTION
        assert "NOT query=" in INSTRUCTION
        assert "NO_DATA_WARNING" in INSTRUCTION

    def test_observability_prompt_drops_the_monitors_limit_mandate(self):
        from app.sub_agents.tier1.datadog_observability import INSTRUCTION

        assert "Always pass limit=10 to search_datadog_monitors" not in INSTRUCTION
        assert "search_datadog_monitors REJECTS limit" in INSTRUCTION

    def test_observability_prompt_anchors_every_tool_not_just_spans(self):
        from app.sub_agents.tier1.datadog_observability import INSTRUCTION

        assert "only SOME tools accept a time window" not in INSTRUCTION
        assert "aggregate_spans only" not in INSTRUCTION
        assert "EVERY time-scoped Datadog tool accepts a window" in INSTRUCTION
        assert "reserved word" in INSTRUCTION.lower()

    def test_observability_prompt_forbids_the_silently_dropped_kwargs(self):
        from app.sub_agents.tier1.datadog_observability import INSTRUCTION

        assert "NEVER use start= / end=" in INSTRUCTION
        assert "SILENTLY DISCARDS" in INSTRUCTION

    def test_prompts_pass_computes_and_never_a_phantom_limit_to_aggregate_spans(self):
        """Per servers/datadog_token.pyi (readToolFile, 2026-07-28):

            def aggregate_spans(computes: list[dict], query: str, telemetry: dict,
                                from: str = None, group_by: dict = None,
                                max_tokens: float = None, start_at: float = None,
                                to: str = None) -> dict

        `computes` is REQUIRED (no default) and there is NO `limit` parameter — the
        prompts used to mandate `limit=10`.
        """
        from app.sub_agents.tier1.datadog_observability import (
            INSTRUCTION as OBS_INSTRUCTION,
        )
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as TIERB_INSTRUCTION

        for instruction in (TIERB_INSTRUCTION, OBS_INSTRUCTION):
            assert '"computes"' in instruction or "computes=" in instruction
            assert "aggregation" in instruction
            assert "limit=10" not in instruction
            assert "aggregate_spans only" not in instruction


class TestAnalyzeDatadogLogsCallForm:
    """analyze_datadog_logs: the ONLY tool that returns aggregated LOG numbers.

    EVIDENCE TIER: LIVE-VERIFIED 2026-07-28 via Bifrost -> Datadog MCP.
      - Called with no args: `required: missing properties: ["sql_query"]`.
      - `query=` is rejected: `unexpected additional properties ["query"]`.
      - `sql_query=` + dict-unpacked from/to SUCCEEDS and the window really moves:
        SELECT min(timestamp), max(timestamp) FROM logs with
        {"from": "2026-07-27T13:00:00Z", "to": "2026-07-27T14:00:00Z"}
        returned mn 13:00:00.005Z / mx 13:59:59.995Z, n 1,343,692.
      - `start=`/`end=` alongside sql_query: rejected outright
        (`unexpected additional properties ["end" "start"]`).
    """

    def test_prompts_teach_sql_query_not_query(self):
        from app.sub_agents.tier1.datadog_observability import (
            INSTRUCTION as OBS_INSTRUCTION,
        )
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as TIERB_INSTRUCTION

        for instruction in (TIERB_INSTRUCTION, OBS_INSTRUCTION):
            assert "sql_query" in instruction
            assert "FROM logs" in instruction

    def test_prompts_document_from_iso8601_timestamp_as_unsupported(self):
        """Live: `unrecognized function 'from_iso8601_timestamp' at 1:50` (400)."""
        from app.sub_agents.tier1.datadog_observability import (
            INSTRUCTION as OBS_INSTRUCTION,
        )
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as TIERB_INSTRUCTION

        for instruction in (TIERB_INSTRUCTION, OBS_INSTRUCTION):
            assert "from_iso8601_timestamp" in instruction
            assert "NOT a recognised function" in instruction

    def test_prompts_warn_the_sql_predicate_cannot_move_the_window(self):
        """The trap: `WHERE timestamp >= TIMESTAMP '...'` parses but only intersects.

        Live-measured: a 7-day predicate returned 0 rows while the same query with no
        predicate returned 995,549; min/max under a wide predicate returned today's
        rolling hour and slid forward on each call.
        """
        from app.sub_agents.tier1.datadog_observability import (
            INSTRUCTION as OBS_INSTRUCTION,
        )
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as TIERB_INSTRUCTION

        for instruction in (TIERB_INSTRUCTION, OBS_INSTRUCTION):
            assert "Do NOT put the window in the SQL" in instruction

    def test_prompts_prefer_it_over_search_datadog_logs_for_numbers(self):
        """Task requirement: it is the PREFERRED tool for log evidence."""
        from app.sub_agents.tier1.datadog_observability import (
            INSTRUCTION as OBS_INSTRUCTION,
        )
        from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as TIERB_INSTRUCTION

        for instruction in (TIERB_INSTRUCTION, OBS_INSTRUCTION):
            lowered = instruction.lower()
            assert "prefer" in lowered and "analyze_datadog_logs" in instruction
            assert "raw message text" in lowered

    def test_sql_timestamp_literal_without_from_to_is_flagged(self):
        code = (
            'result = datadog_token.analyze_datadog_logs(sql_query="SELECT count(*) AS n '
            "FROM logs WHERE timestamp >= TIMESTAMP '2026-07-27 13:00:00' "
            "AND timestamp < TIMESTAMP '2026-07-27 14:00:00'\")"
        )
        assert _sql_only_window_calls(code) == ["analyze_datadog_logs"]
        assert _unconstrained_time_calls(code) == ["analyze_datadog_logs"]

    def test_sql_timestamp_literal_WITH_from_to_is_not_flagged(self):
        code = (
            'args = {"sql_query": "SELECT count(*) AS n FROM logs", '
            '"from": "2026-07-27T13:00:00Z", "to": "2026-07-27T14:00:00Z"}\n'
            "result = datadog_token.analyze_datadog_logs(**args)"
        )
        assert _sql_only_window_calls(code) == []
        assert _unconstrained_time_calls(code) == []

    def test_the_verified_working_call_is_clean(self):
        """The exact call form proven live to return incident-window log evidence."""
        code = (
            'args = {"sql_query": "SELECT status, count(*) AS n FROM logs GROUP BY status", '
            '"filter": "service:acme.example", '
            '"from": "2026-07-27T13:00:00Z", "to": "2026-07-27T14:00:00Z"}\n'
            "result = datadog_token.analyze_datadog_logs(**args)\nprint(result)"
        )
        assert _unconstrained_time_calls(code) == []
        assert _dropped_kwarg_calls(code) == []
        assert _sql_only_window_calls(code) == []


class TestOutOfRetentionDifferential:
    """The one test that distinguishes "kwarg ACCEPTED" from "kwarg HONORED".

    Every other check can be satisfied by a call that returns plausible data. This one
    cannot: it asks for a 5-minute window in Jan 2025, far outside the org's 90-day APM
    span retention. A tool that HONOURS the window must return nothing; a tool that
    silently discards it returns millions of all-time spans.

    LIVE DIFFERENTIAL, 2026-07-28, identical query "*" and computes:
      from/to   -> total_buckets 0, warnings ["Requested time range (2025-01-01T00:00:00Z
                   to 2025-01-01T00:05:00Z) is entirely outside this org's APM span
                   retention window: spans older than 90 days (before 2026-04-29T20:31:17Z)
                   are no longer retained. Retry with a more recent time range."]
      start/end -> total_buckets 1, count 3,319,840

    3.3 million spans attributed to a five-minute window in 2025. If the guard ever again
    treats start=/end= as a constraint, the agent will cite that number as incident
    evidence and the warning will be suppressed. Hence this is pinned as a unit-level
    contract on the detector, not just documented in a comment.
    """

    OUT_OF_RETENTION = ("2025-01-01T00:00:00Z", "2025-01-01T00:05:00Z")

    def test_start_end_over_a_dead_window_is_still_flagged(self):
        """The 3.3M-span call. It MUST NOT read as constrained."""
        lo, hi = self.OUT_OF_RETENTION
        code = (
            'result = datadog_token.aggregate_spans(query="*", '
            'computes=[{"aggregation": "count"}], '
            f'start="{lo}", end="{hi}")'
        )
        assert _unconstrained_time_calls(code) == ["aggregate_spans"]
        assert _dropped_kwarg_calls(code) == ["aggregate_spans"]

        label = _window_label(_unconstrained_time_calls(code), ALERT_TS)
        assert "!! UNCONSTRAINED TIME WINDOW" in label

    def test_from_to_over_a_dead_window_is_accepted_as_constrained(self):
        """The honest call: it returns 0 buckets + a retention warning, and is NOT flagged."""
        lo, hi = self.OUT_OF_RETENTION
        code = (
            'args = {"query": "*", "computes": [{"aggregation": "count"}], '
            f'"from": "{lo}", "to": "{hi}"}}\n'
            "result = datadog_token.aggregate_spans(**args)"
        )
        assert _unconstrained_time_calls(code) == []
        assert _dropped_kwarg_calls(code) == []

    def test_the_two_forms_are_classified_differently(self):
        """The whole point: same tool, same window, opposite verdicts.

        Identical intent, and only the parameter NAME differs. If these two ever
        classify the same way, the guard has stopped distinguishing accepted from
        honored — which is the failure this suite exists to prevent.
        """
        lo, hi = self.OUT_OF_RETENTION
        honored = (
            'args = {"query": "*", "computes": [{"aggregation": "count"}], '
            f'"from": "{lo}", "to": "{hi}"}}\n'
            "result = datadog_token.aggregate_spans(**args)"
        )
        discarded = (
            'result = datadog_token.aggregate_spans(query="*", '
            f'computes=[{{"aggregation": "count"}}], start="{lo}", end="{hi}")'
        )
        assert _unconstrained_time_calls(honored) != _unconstrained_time_calls(discarded)


class TestSilentlyDroppedKwargDetection:
    """start=/end= is accepted by the span tools and DISCARDED — the worst failure mode.

    Live-measured 2026-07-28, one fixed aggregate_spans query, sequential:
        no window 514000 | start=/end= 513995 | from/to 467682 | no window 514094
    Confirmed the same way for search_datadog_spans (start/end returned 2026-07-28 spans
    for a requested 2026-07-20 window) and search_datadog_error_tracking_issues
    (start/end total_counts identical to the unwindowed call).
    """

    def test_start_end_is_detected_as_dropped(self):
        code = (
            "result = datadog_token.aggregate_spans(query='service:x', "
            "start='2026-07-20T13:00:00Z', end='2026-07-20T14:00:00Z')"
        )
        assert _dropped_kwarg_calls(code) == ["aggregate_spans"]

    def test_from_ts_to_ts_is_detected_as_dropped(self):
        code = "result = datadog_token.search_datadog_spans(query='x', from_ts=1, to_ts=2)"
        assert _dropped_kwarg_calls(code) == ["search_datadog_spans"]

    def test_a_correct_from_to_call_is_not_flagged_as_dropped(self):
        code = (
            'args = {"query": "x", "from": "2026-07-20T13:00:00Z", '
            '"to": "2026-07-20T14:00:00Z"}\n'
            "result = datadog_token.aggregate_spans(**args)"
        )
        assert _dropped_kwarg_calls(code) == []

    def test_acme_net_does_not_trip_the_dropped_detector(self):
        """Guards the same 'end' substring regression, in the new detector."""
        code = 'result = datadog_token.search_datadog_spans(query="service:acme.example")'
        assert _dropped_kwarg_calls(code) == []

    def test_dropped_kwarg_warning_reaches_the_model(self):
        """A silently-dropped window must produce a warning naming the real mistake."""
        import asyncio
        from unittest.mock import patch

        from app.sub_agents.tier1 import datadog_tierb

        code = (
            "result = datadog_token.aggregate_spans(query='service:x', "
            "start='2026-07-20T13:00:00Z', end='2026-07-20T14:00:00Z')"
        )

        # Returns (results, codes_run, notes) — the executor may rewrite a boolean
        # Sentry query into several, so callers zip against the codes actually run.
        async def fake_execute(codes):
            return ["count 514000"] * len(codes), list(codes), []

        with patch.object(datadog_tierb, "bifrost_execute_many_annotated", fake_execute):
            out = asyncio.run(datadog_tierb.fetch_datadog([code], _Ctx(
                {"classification": {"alert_timestamp": ALERT_TS}})))

        assert "!! UNCONSTRAINED TIME WINDOW" in out
        assert "TIME ARGUMENT SILENTLY DROPPED" in out
        assert "aggregate_spans(**args)" in out

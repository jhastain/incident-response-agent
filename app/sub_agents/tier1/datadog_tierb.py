"""Datadog Tier B — one ADK LlmAgent + one async batch FunctionTool.

Removes the Bifrost discovery dance and the multi-round ReAct loop WITHOUT the
structural problems the earlier attempts hit:
  - inline model.generate_content_async() from a custom BaseAgent crashed with
    "ContextVar was created in a different Context" (ADK's ContextVar machinery
    requires the model to be driven by ADK's own agent invocation, not called
    manually).
  - nested SequentialAgent / a BaseAgent driving child.run_async() inside the
    tier1 ParallelAgent got aclose()d mid-await -> BaseExceptionGroup, wedging
    the whole parallel phase.

This design is a single ordinary LlmAgent (so ADK drives the model and
ContextVars stay correct) that has ONE tool: fetch_datadog(queries) — an async
FunctionTool that runs every planned datadog_token.* call CONCURRENTLY via httpx
in a single batch. The agent: plan the query list -> call fetch_datadog ONCE ->
summarize into findings_datadog. No discovery meta-tools, no per-query ReAct
round-trips, no nested agents, no inline model calls.
"""

from __future__ import annotations

import json
import re
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...config import create_fast_model
from ...tools.bifrost_direct import bifrost_execute_many_annotated
from ...tools.tool_resilience import keep_running_on_tool_error

# Tools whose results are only meaningful relative to a time range. A call to one of
# these WITHOUT a time argument silently uses the Datadog default (the last hour =
# "now"), which produced a report citing 43,525 spans for an 8-hour-old incident
# window that was never queried. Time-scoped by nature; must be constrained.
_TIME_SCOPED_TOOLS = (
    "search_datadog_logs",
    "analyze_datadog_logs",
    "aggregate_spans",
    "search_datadog_spans",
    "get_datadog_metric",
    "search_datadog_events",
    "search_datadog_error_tracking_issues",
)

# EVERY time-scoped tool accepts a window. The parameters are `from` and `to`.
#
# EVIDENCE TIER: DECLARED SIGNATURES, read live 2026-07-28 via
# readToolFile(fileName="servers/datadog_token.pyi") — the authoritative contract, NOT
# inference from probe responses. The distinction is the whole lesson here: a probe that
# returns data does not prove the kwarg was honored (see the retention differential
# below), and that is exactly how start=/end= came to be recorded as "LIVE-VERIFIED".
# Cross-checked against getToolDocs(server="datadog_token", tool=...) — identical.
# Verbatim signatures:
#
#   analyze_datadog_logs(sql_query, telemetry, extra_columns, filter, from, indexes,
#                        max_tokens, start_at, storage_tier, to)
#   search_datadog_logs(query, telemetry, clustering_pattern_field, extra_fields, from,
#                       indexes, max_tokens, pattern_group_by, sort, start_at,
#                       storage_tier, to, use_log_patterns)
#   search_datadog_events(telemetry, from, max_tokens, query, sort, start_at, to)
#   get_datadog_metric(queries, telemetry, formulas, from, interval, max_tokens,
#                      raw_data, response_format, to, use_cloud_cost)
#   aggregate_spans(computes, query, telemetry, from, group_by, max_tokens, start_at, to)
#   search_datadog_spans(query, telemetry, custom_attributes, from, max_tokens, sort,
#                        start_at, to)
#   search_datadog_error_tracking_issues(telemetry, assignee, from, max_tokens, order_by,
#                                        query, states, team, to)
#
# Note what is NOT in any of those signatures: `start`, `end`, `from_ts`, `to_ts`.
#
# The long-standing "these tools reject every time argument" finding was a MEASUREMENT
# ARTEFACT of probing with the wrong parameter names. `from` is a RESERVED WORD in
# Python and in Bifrost's Starlark sandbox, so `from="..."` cannot be written as a
# literal kwarg — it is a syntax error. Earlier probes therefore reached for
# start=/end=/from_ts=/to_ts=, got rejections, and concluded the tools were
# non-windowable. They are not. Pass the window by dict-unpacking instead:
#
#   args = {"query": "service:x", "from": "<ISO8601>", "to": "<ISO8601>"}
#   result = datadog_token.aggregate_spans(**args)
#
# Live proof the window really MOVES (identical query, sequential calls so the rolling
# default barely drifts) — datadog_token.aggregate_spans(query="service:acme.example"):
#   no window            -> count 514000
#   start=/end=          -> count 513995   <-- tracks the DEFAULT, not the requested window
#   from/to (2026-07-20) -> count 467682   <-- the only form that moved it
#   no window (again)    -> count 514094
# Same result for search_datadog_spans (start/end returned 2026-07-28 spans for a
# requested 2026-07-20 window; from/to returned 2026-07-20 spans) and for
# search_datadog_error_tracking_issues (start/end total_counts identical to the
# unwindowed call; from/to returned a different set).
#
# So `start=`/`end=` on the span tools is WORSE than passing nothing: Bifrost accepts the
# call and SILENTLY DROPS the arguments, returning last-hour data that looks windowed.
# That is precisely the defect this guard exists to catch, and the prompts used to
# mandate exactly that form.
#
# THE DECISIVE TEST — an out-of-retention window, where an honored bound MUST return
# nothing. Same query "*", same computes, 5 minutes in Jan 2025:
#   from/to   -> total_buckets 0 + warning "Requested time range (2025-01-01T00:00:00Z to
#                2025-01-01T00:05:00Z) is entirely outside this org's APM span retention
#                window: spans older than 90 days (before 2026-04-29T20:31:17Z) are no
#                longer retained."
#   start/end -> total_buckets 1, count 3,319,840
# 3.3 million spans attributed to a five-minute window in 2025. This differential is the
# only check that separates "the server ACCEPTED the kwarg" from "the server HONORED it",
# and it is pinned in tests/unit/test_datadog_time_window.py::TestOutOfRetentionDifferential.
_WINDOWABLE_TOOLS = frozenset({
    "aggregate_spans",
    "search_datadog_spans",
    "search_datadog_error_tracking_issues",
    "analyze_datadog_logs",
    "search_datadog_logs",
    "search_datadog_events",
    "get_datadog_metric",
})

# An EFFECTIVE time constraint is `from`/`to` — the only names in any Datadog tool
# signature (live getToolDocs, 2026-07-28). Because `from` is a reserved word it cannot
# appear as a literal kwarg, so the real call form is dict-unpacked and the constraint
# shows up as a QUOTED DICT KEY:
#     args = {"query": "...", "from": "<ISO>", "to": "<ISO>"}
#     result = datadog_token.aggregate_spans(**args)
# Hence this matches `"from":` / `'to':` as well as a literal `to=` kwarg.
#
# Deliberately NOT matched: start=, end=, from_ts=, to_ts=, start_time=, end_time=.
# Those are not parameters of any Datadog tool. Bifrost either rejects them outright or —
# on the span tools — ACCEPTS AND SILENTLY DROPS them, returning last-hour data that
# looks windowed (measured: aggregate_spans start/end 513995 vs no-window 514000 vs
# from/to 467682). Treating them as a constraint would suppress the warning on exactly
# the calls that most need it, so they must stay unmatched.
#
# Matching is on whole tokens, NOT loose substrings: an earlier version tested for the
# bare substring "end", which matched inside "service:acme.example" and let the
# offending call through as if it were constrained.
_TIME_KWARG_RE = re.compile(
    r"""(?:["']\s*(?:from|to)\s*["']\s*:|\bto\s*=)""",
    re.IGNORECASE,
)

# `start_at` is a PAGINATION cursor on these tools, not a time bound. It must never be
# read as a window; listed here only to document why it is absent from _TIME_KWARG_RE.
_NOT_A_TIME_BOUND = ("start_at", "start", "end", "from_ts", "to_ts")

# In-query range syntax: @timestamp:[X TO Y]. VERIFIED INEFFECTIVE on the live MCP
# server (2026-07-27 e2e): a query carrying
#   @timestamp:[2026-07-21T20:14:26Z TO 2026-07-21T20:59:26Z]
# came back with logs_explorer_url from_ts/to_ts = 2026-07-27T17:21-18:21Z — the server
# dropped the range and used its own default. Treating this as a valid constraint was
# strictly worse than not detecting it at all: the query silently ran against "now"
# AND the warning was suppressed, so it looked constrained. It is now NOT accepted as
# a constraint; only real kwargs count.
_TIME_IN_QUERY_RE = re.compile(r"@\w*timestamp\w*\s*:\s*\[", re.IGNORECASE)

# `from`/`to` accept a relative bound ("now-1h") as well as ISO 8601 — per the live
# signature docs. Only meaningful as a from/to VALUE, which _TIME_KWARG_RE already
# matches; a bare "now-1h" sitting in a query string is not a time bound.
_RELATIVE_BOUND_RE = re.compile(r"\bnow\s*-\s*\d+[smhd]\b", re.IGNORECASE)

# start=/end= (and from_ts=/to_ts=) on the SPAN tools: accepted by Bifrost, silently
# DROPPED, result is last-hour data wearing an incident-window label. Detected separately
# so the warning can name the actual mistake instead of just "no window".
_DROPPED_KWARG_RE = re.compile(
    r"\b(?:start|end|from_ts|to_ts|start_time|end_time|start_ts|end_ts)\s*=",
    re.IGNORECASE,
)

# A SQL `timestamp >= TIMESTAMP '...'` predicate inside sql_query. This parses and runs,
# but it does NOT move the window — see _sql_only_window_calls below.
_SQL_TIMESTAMP_LITERAL_RE = re.compile(
    r"\btimestamp\s+'[^']+'", re.IGNORECASE
)


def _unconstrained_time_calls(code: str) -> list[str]:
    """Return the time-scoped tools called in `code` with no EFFECTIVE time constraint.

    Only `from`/`to` count — as dict keys (`{"from": ...}`) or a literal `to=`, since
    `from` is a reserved word and cannot be written as a bare kwarg. Nothing else does:
      - `start=`/`end=` are silently dropped by the span tools (measured);
      - an in-query `@timestamp:[...]` range is dropped by the server;
      - a SQL `TIMESTAMP '...'` predicate only intersects the rolling default window.

    Conservative by design: a false positive only adds a warning line, while a false
    negative lets a now-scoped number be reported as incident evidence. Bias to flag.
    """
    lowered = code.lower()
    if _TIME_KWARG_RE.search(code):
        return []
    return [tool for tool in _TIME_SCOPED_TOOLS if tool in lowered]


def _ineffective_range_calls(code: str) -> list[str]:
    """Time-scoped calls relying ONLY on an in-query @timestamp range the server drops."""
    if _TIME_KWARG_RE.search(code):
        return []
    if not _TIME_IN_QUERY_RE.search(code):
        return []
    lowered = code.lower()
    return [tool for tool in _TIME_SCOPED_TOOLS if tool in lowered]


def _dropped_kwarg_calls(code: str) -> list[str]:
    """Time-scoped calls whose ONLY window is a kwarg the server accepts then discards.

    This is the most dangerous form in the whole guard, because it fails silently: the
    call succeeds, returns plausible numbers, and those numbers are the last hour. Live
    measurement on aggregate_spans (same query, sequential):
        no window 514000 | start=/end= 513995 | from/to 467682 | no window 514094
    """
    if _TIME_KWARG_RE.search(code):
        return []
    if not _DROPPED_KWARG_RE.search(code):
        return []
    lowered = code.lower()
    return [tool for tool in _TIME_SCOPED_TOOLS if tool in lowered]


def _sql_only_window_calls(code: str) -> list[str]:
    """analyze_datadog_logs relying on a SQL TIMESTAMP predicate instead of from/to.

    A `WHERE timestamp >= TIMESTAMP '...'` predicate is valid DDSQL and runs, but the
    'logs' table it queries has ALREADY been narrowed to the tool's rolling ~1h default.
    The predicate can only INTERSECT that window; it cannot move it to an old incident.

    Live-measured 2026-07-28:
      - `SELECT min(timestamp), max(timestamp), count(*) FROM logs` with NO from/to
        returned mn 2026-07-28T19:08:33Z / mx 2026-07-28T20:08:10Z — a 1-hour span that
        SLID FORWARD on each subsequent call (19:09:15.., 19:10:49..), i.e. rolling now.
      - Adding `WHERE timestamp >= TIMESTAMP '2026-07-01 00:00:00'` returned the SAME
        mn/mx and the SAME count — the predicate changed nothing.
      - A 7-day predicate covering 2026-07-21..28 returned 0 rows, while the identical
        query with no predicate returned 995,549.
      - With `{"from": "2026-07-27T13:00:00Z", "to": "...T14:00:00Z"}` the same min/max
        query returned exactly 13:00:00.005Z .. 13:59:59.995Z — the window really moved.
    """
    if "analyze_datadog_logs" not in code.lower():
        return []
    if _TIME_KWARG_RE.search(code):
        return []
    if not _SQL_TIMESTAMP_LITERAL_RE.search(code):
        return []
    return ["analyze_datadog_logs"]


def _alert_timestamp(tool_context: Any) -> str:
    """Best-effort read of classification.alert_timestamp from session state.

    Returns "" when unavailable — the classifier legitimately emits the literal
    string "unknown" for payloads with no epoch (e.g. a raw Datadog monitor body),
    so callers must treat absence as normal, not as an error.
    """
    state = getattr(tool_context, "state", None)
    if state is None:
        return ""
    try:
        classification = state.get("classification")
    except Exception:
        return ""
    if isinstance(classification, str):
        try:
            classification = json.loads(re.search(r"\{.*\}", classification, re.S).group(0))
        except Exception:
            return ""
    if not isinstance(classification, dict):
        return ""
    ts = str(classification.get("alert_timestamp") or "").strip()
    return "" if ts.lower() in {"", "unknown", "none", "null"} else ts


def _reissue_form(tool: str, alert_ts: str) -> str:
    """The exact dict-unpacked call the model should re-issue. Never a bare `from=`."""
    first = "sql_query" if tool == "analyze_datadog_logs" else (
        "queries" if tool == "get_datadog_metric" else "query"
    )
    example = (
        '["<metric query>"]' if tool == "get_datadog_metric"
        else '"SELECT count(*) AS n FROM logs"' if tool == "analyze_datadog_logs"
        else '"<filters>"'
    )
    lo = f"{alert_ts} - 30min" if alert_ts else "<from>"
    hi = f"{alert_ts} + 15min" if alert_ts else "<to>"
    return (
        f'args = {{"{first}": {example}, "from": "{lo}", "to": "{hi}"}}; '
        f"result = datadog_token.{tool}(**args)"
    )


def _window_label(offenders: list[str], alert_ts: str) -> str:
    """Precedence label for a time-scoped call that carried no EFFECTIVE window.

    Precedence (see ADO-200004):
      1. explicit window  -> handled by the caller; this function is not reached
      2. no window + known alert timestamp -> DERIVED from the incident time
      3. neither -> last-hour default
    Never rejects the call; always returns data with the window it actually used
    named, so the verifier can tell incident-window evidence from "now" data.

    Every time-scoped Datadog tool accepts `from`/`to` (live getToolDocs 2026-07-28), so
    every offender is re-issuable. There is no longer an unfixable class: the previous
    "this tool REJECTS every time argument" branch was an artefact of probing with
    parameter names no tool declares (start/end/from_ts/to_ts) — `from` is a reserved
    word and had never been tried via dict-unpacking.

    Every branch carries the literal "!! UNCONSTRAINED TIME WINDOW": synthesis.py keys on
    that exact string to refuse a now-scoped number as incident evidence. The result AS
    RETURNED is still last-hour data, so dropping the marker would quietly open the very
    hole this guard closes.
    """
    tools = ", ".join(offenders)
    forms = "  ".join(_reissue_form(t, alert_ts) for t in offenders)

    if alert_ts:
        return (
            f"\n!! UNCONSTRAINED TIME WINDOW — WINDOW DERIVED FROM ALERT TIMESTAMP: "
            f"{tools} carried no EFFECTIVE time argument. The incident time is known "
            f"({alert_ts}), so re-issue with bounds of ({alert_ts} - 30min) .. "
            f"({alert_ts} + 15min) before citing it. Pass the window as `from`/`to` via "
            f"dict-unpacking — `from` is a reserved word, so `from=` is a syntax error, "
            f"and `start=`/`end=` are silently DROPPED by the server: {forms}. "
            "As returned, this result covers the Datadog DEFAULT window (~last hour = NOW), "
            "NOT the incident window — do not cite these numbers as incident evidence."
        )
    return (
        f"\n!! UNCONSTRAINED TIME WINDOW (LAST-HOUR DEFAULT): {tools} was called with no "
        "EFFECTIVE time argument AND the alert timestamp is unknown, so this result covers "
        "the Datadog DEFAULT window (roughly the last hour = NOW), NOT the incident window. "
        "This is the documented fallback, not an error — but you MUST label any figure taken "
        "from it as 'current (last hour), not incident-window'. Do NOT present it as incident "
        f"evidence. If you learn the incident time, re-issue with `from`/`to`: {forms}"
    )


async def fetch_datadog(queries: list[str], tool_context: Any = None) -> str:
    """Execute Datadog queries concurrently via Bifrost and return combined results.

    Args:
        queries: A list of complete Python code strings, each a Datadog call
            against the `datadog_token` server. Every time-scoped call MUST carry an
            explicit time window anchored on the alert timestamp — an unconstrained
            call silently returns "the last hour", i.e. now, not the incident.
            Pass ALL queries in ONE call — they run in parallel.
        tool_context: Injected by ADK. Supplies the alert timestamp so a missing
            window can be labelled as DERIVED rather than a blind last-hour default.

    Returns:
        Combined raw results, one block per query. Any query that omitted a time
        window is prefixed with an explicit WARNING naming the window actually used,
        so it can never be misreported downstream as the incident window.
    """
    if not queries:
        return "No queries provided."
    alert_ts = _alert_timestamp(tool_context) if tool_context is not None else ""
    # `codes` is rebound to what actually ran — the executor may rewrite a boolean
    # Sentry query into several, and the per-query warnings below inspect the code
    # string, so they must inspect the one that was really sent.
    results, codes, rewrite_notes = await bifrost_execute_many_annotated(
        [str(q) for q in queries]
    )
    blocks = []
    if rewrite_notes:
        blocks.append(
            "### NOTE — queries were rewritten before execution\n" + "\n".join(rewrite_notes)
        )
    for i, (q, r) in enumerate(zip(codes, results)):
        offenders = _unconstrained_time_calls(q)
        ineffective = _ineffective_range_calls(q)
        dropped = _dropped_kwarg_calls(q)
        sql_only = _sql_only_window_calls(q)
        warning = ""
        if dropped:
            # Most dangerous case: the call SUCCEEDED and looks windowed, but the server
            # discarded the bounds. Name the mistake precisely rather than just "no window".
            warning = _window_label(offenders or dropped, alert_ts) + (
                "\n!! TIME ARGUMENT SILENTLY DROPPED: "
                f"{', '.join(dropped)} was given start=/end= (or from_ts=/to_ts=). No Datadog "
                "tool declares those parameters — the server ACCEPTED the call and IGNORED "
                "them, so the numbers above are the rolling last hour despite looking "
                "windowed. Measured 2026-07-28 on aggregate_spans with one fixed query: "
                "no-window 514000, start=/end= 513995, from/to 467682, no-window 514094. "
                "The ONLY parameters that move the window are `from` and `to`."
            )
        elif sql_only:
            warning = _window_label(offenders or sql_only, alert_ts) + (
                "\n!! SQL TIMESTAMP PREDICATE DOES NOT MOVE THE WINDOW: analyze_datadog_logs "
                "was given a `timestamp >= TIMESTAMP '...'` filter but no `from`/`to`. The "
                "virtual 'logs' table is ALREADY narrowed to the rolling ~1h default, so a SQL "
                "predicate can only INTERSECT that window — it cannot reach an older incident. "
                "Measured 2026-07-28: a 7-day predicate returned 0 rows while the same query "
                "with no predicate returned 995,549; min/max with a wide predicate returned "
                "today's last hour and slid forward on each call. Pass the window as "
                "`{\"from\": ..., \"to\": ...}` instead."
            )
        elif offenders:
            warning = _window_label(offenders, alert_ts)
        elif ineffective:
            warning = (
                "\n!! TIME RANGE IGNORED BY SERVER: "
                f"{', '.join(ineffective)} passed its range inside the query string "
                "(@timestamp:[...]) only. The MCP server DROPS that and uses its default "
                "window (~last hour = NOW) — verified 2026-07-27. This result does NOT "
                "cover the window you asked for. Re-issue using real time ARGUMENTS — the "
                "parameters are `from`/`to`, passed by dict-unpacking (you have no "
                "getToolDocs tool; do not try to look them up) — or report the figure "
                "as 'current (last hour), not incident-window'. Check the result's own "
                "from_ts/to_ts or logs_explorer_url to see the window actually used."
            )
        blocks.append(f"### Query {i+1}\nCODE: {q[:200]}{warning}\nRESULT:\n{r}")
    return "\n\n".join(blocks)


fetch_datadog_tool = FunctionTool(func=fetch_datadog)

INSTRUCTION = """\
You are the Datadog Observability Investigator. You have ONE tool: fetch_datadog(queries).

## Context
- Classification: {classification}
- Service context: {service_context}

## YOU HAVE NO BIFROST DISCOVERY META-TOOLS — do not plan or narrate one

`executeToolCode`, `listToolFiles`, `getToolDocs`, and `readToolFile` are NOT declared to
you. `fetch_datadog` is your only tool, and it already runs your `datadog_token.*` code
strings for you — call it directly. Every parameter name you need is documented below, so
there is nothing to discover first: never write a plan that "lists tool files" or "reads
the tool docs", and never narrate the output of a discovery step you did not take.

Measured 2026-08-07 across 7 sessions: 6 `executeToolCode` calls were emitted by Tier-B
agents, one of them by this one, each immediately preceded in the same turn by invented
narration of the form `The Output from allowed tools calls is - {"listToolFiles": ...}`.
The model wrote the fiction first, then called the meta-tool to complete it. ADK rejects
the call — `Tool 'executeToolCode' not found` — so it returns no data, and the wasted slot
still counts against the 6-call investigation budget: roughly 17% of the budget spent on a
tool that does not exist. The signature notes above cite `getToolDocs`/`readToolFile` as
the SOURCE of the parameter facts; that was a one-time offline verification, not a call
available to you.

## How you work (do this exactly)
0. COMPUTE THE WINDOW FIRST, before writing any query. Read
   {{classification}}.alert_timestamp and write out, literally, the two bounds you will use:
   `WINDOW = <alert_timestamp - 30min> .. <alert_timestamp + 15min>` (UTC, ISO 8601).
   Carry those bounds on EVERY time-scoped query — they all support it. If
   alert_timestamp is "unknown", write `WINDOW = last 1h (alert_timestamp unknown)` and
   label every finding from it as "current, not incident-window".
1. PLAN the Datadog queries as a list of Python code strings against `datadog_token`.
2. Call fetch_datadog(queries=[...]) ONE time with ALL of them — they run concurrently.
3. SUMMARIZE the returned results into findings. Do NOT call the tool again unless a
   result shows a clear, fixable query error, or carries a `!! UNCONSTRAINED TIME WINDOW`
   / `!! TIME ARGUMENT SILENTLY DROPPED` / `!! SQL TIMESTAMP PREDICATE` flag — those you
   SHOULD re-issue with a real `from`/`to` window.

## CRITICAL — the window parameters are `from` and `to`, passed by dict-unpacking

EVERY time-scoped Datadog tool accepts a window. The parameters are **`from`** and **`to`**
(ISO 8601, or relative like `now-1h`). Live-verified 2026-07-28 against the real schema.

`from` is a RESERVED WORD in Python/Starlark, so `from="..."` is a SYNTAX ERROR. You MUST
build an args dict and unpack it:

    args = {"query": "service:acme.example", "from": "2026-07-27T13:00:00Z", "to": "2026-07-27T14:00:00Z"}
    result = datadog_token.aggregate_spans(**args)
    print(result)

**NEVER use `start=` / `end=` / `from_ts=` / `to_ts=`.** No Datadog tool declares them. On
the span tools the server ACCEPTS the call and SILENTLY DISCARDS them, handing you
last-hour data that looks windowed. Measured on one fixed aggregate_spans query:
  no window 514000 | `start=`/`end=` 513995 | `from`/`to` 467682 | no window 514094
Only `from`/`to` moved the window. A `start=`/`end=` call is worse than no window at all,
because it hides the problem.

Putting the range inside the query string as `@timestamp:[X TO Y]` does NOT work either —
the server silently drops it.

`search_datadog_monitors` is the one exception: it has NO window parameter (monitors carry
current state, not a time series). It also rejects `limit`; scope it with `query=` alone.

**`get_datadog_metric` takes `queries=` — a LIST of strings — not `query=`.** Live-verified
2026-07-28. `query=` is rejected outright ("unexpected additional properties"); omitting it
reports `required: missing properties: ["queries"]`. It takes `from`/`to` like the rest:
  `args = {"queries": ['avg:trace.job.scheduled.errors{env:prod}'], "from": "<from>", "to": "<to>"}`
  `result = datadog_token.get_datadog_metric(**args)`
Two things this tool returns that are FINDINGS, not failures:
- `AUTO_MODIFIERS` — the server may apply `.as_count()` automatically for count-type
  metrics. Report the modifier alongside the number; it changes what the number means.
- `NO_DATA_WARNING` — the query ran fine and legitimately has no points in range. That is
  real evidence (it rules a hypothesis out). Report it as "no data points", never as an error.

**So when a result comes back flagged:**
- `WINDOW DERIVED FROM ALERT TIMESTAMP` — re-issue that ONE query with real `from`/`to`.
- `!! TIME ARGUMENT SILENTLY DROPPED` — you used `start=`/`end=`. The call succeeded but the
  bounds were discarded. Re-issue with `{"from": ..., "to": ...}` unpacked.
- `!! SQL TIMESTAMP PREDICATE DOES NOT MOVE THE WINDOW` — a SQL `timestamp >= TIMESTAMP '...'`
  filter only narrows WITHIN the default hour. Add real `from`/`to` and re-issue.
- `!! TIME RANGE IGNORED BY SERVER` — an in-query `@timestamp:[...]` range. Same fix.

Every one of those flags means the number you got is last-hour data. Whichever flag you
see, that figure is never incident-window evidence until a re-issue actually returns the
incident window.

**ALWAYS confirm the window you actually got.** Results carry the window they used —
`from_ts`/`to_ts`, or a `logs_explorer_url` containing them. Read it back. If it differs
from the window you intended, the number describes a different time period; say so rather
than reporting your intended window.

Never silently attach the incident's timestamps to a last-hour number.

## Downstream calls (datadog_token.*), each string must assign `result` and print it
Every one of these takes the window as `{"from": "<from>", "to": "<to>"}` unpacked with `**`.
Write `W = {"from": "<from>", "to": "<to>"}` once at the top of each code string, then merge.

- args = dict(W, **{"query": "<filters>", "computes": [{"aggregation": "count"}]})\\nresult = datadog_token.aggregate_spans(**args)\\nprint(result)
- args = dict(W, **{"query": "<filters>"})\\nresult = datadog_token.search_datadog_spans(**args)\\nprint(result)
- args = dict(W, **{"query": "<filters>"})\\nresult = datadog_token.search_datadog_error_tracking_issues(**args)\\nprint(result)
- args = dict(W, **{"query": "<filters>"})\\nresult = datadog_token.search_datadog_logs(**args)\\nprint(result)
- args = dict(W, **{"query": "<filters>"})\\nresult = datadog_token.search_datadog_events(**args)\\nprint(result)
- args = dict(W, **{"queries": ["<metric query>"]})\\nresult = datadog_token.get_datadog_metric(**args)\\nprint(result)
  ^ note `queries=` and the LIST. `query="..."` is rejected by the server.

LOG AGGREGATES — `analyze_datadog_logs` takes `sql_query=` (NOT `query=`), plus `from`/`to`:
- args = dict(W, **{"sql_query": "SELECT status, count(*) AS n FROM logs GROUP BY status ORDER BY count(*) DESC"})\\nresult = datadog_token.analyze_datadog_logs(**args)\\nprint(result)
  Default columns: timestamp, host, service, env, version, status, message. Optional
  `filter` takes Datadog search syntax (e.g. `"filter": "status:error"`) — use it to scope
  by service rather than a SQL WHERE on service, and let `from`/`to` do the time scoping.
  DDSQL is a PostgreSQL subset: every non-aggregated SELECT column must appear in GROUP BY,
  and aliases cannot be reused (repeat the expression, e.g.
  `GROUP BY DATE_TRUNC('hour', timestamp)`, not `GROUP BY hour`).
  Do NOT put the window in the SQL. `WHERE timestamp >= TIMESTAMP '...'` parses but only
  narrows WITHIN the default hour — it cannot reach an older incident. `from_iso8601_timestamp()`
  is NOT a recognised function (400 error). Use `from`/`to`.

No window parameter (current state / static, correctly called with no bounds):
- result = datadog_token.search_datadog_monitors(query="...")\\nprint(result)
- result = datadog_token.search_datadog_services(query="...")\\nprint(result)
- result = datadog_token.get_datadog_dashboard(dashboard_id="...")\\nprint(result)

## Query rules (same anchoring as before)
- Anchor time on {{classification}}.alert_timestamp: (t-30min) to (t+15min); last 1h if unknown.
  Apply those bounds via `from`/`to` on EVERY time-scoped call — they all accept them.
- Service map: WebCore -> service:acme.example ; FileService -> service:files.acme.example ;
  SubscriptionService -> service:subscriptionservice.
- Env SRE dashboards: prod aaa-111-bbb, sandbox ggg-444-hhh, test eee-333-fff, ephemeral iii-555-jjj.
- Category dashboards: sql-database/scheduled-jobs/data-integrity ccc-222-ddd ;
  pubsub-messaging ooo-888-ppp ; performance mmm-777-nnn ; auth-security qqq-999-rrr.
- Plan 3-6 queries: SRE dashboard, domain dashboard (if any), monitors filter (alert/warn only),
  service+error log search, 1-2 metric/span queries. Do NOT over-query.
- EVERY time-scoped query must carry `from`/`to`. There is no longer any excuse for a
  last-hour number: a run that reports one has simply omitted the window.
- WHEN THE QUESTION IS ABOUT LOGS — error counts, message patterns, log volume, a
  breakdown by status/service — PREFER `analyze_datadog_logs` with `from`/`to` over
  `search_datadog_logs`. It aggregates server-side and returns exact counts for the
  incident window (e.g. `SELECT status, count(*) FROM logs GROUP BY status` over the
  incident hour), whereas `search_datadog_logs` returns a truncated sample you would have
  to count by hand. Use `search_datadog_logs` when you need the raw message TEXT of
  individual log lines; use `analyze_datadog_logs` for any number.

## Findings output
- Report ONLY what results show. Errors/empty -> "No data retrieved from Datadog for <query>".
- STATE THE WINDOW ON EVERY NUMBER. Any count, rate, or duration must be written as
  "<number> (<from> .. <to> UTC)" using the window the QUERY ACTUALLY USED — not the window
  you intended, and not the alert window unless the query truly carried it. A number without
  a window is not a finding.
- TRUNCATION: if a result carries `is_truncated: true` (or shows N of M), report it as
  "showing X of Y — truncated" and never present the shown subset as a total.
  `search_datadog_spans` routinely returns `is_truncated: true` with a `truncation_message`
  inviting another call. A count taken from a truncated response is a LOWER BOUND, not a
  total — write it "at least N (truncated)". Presenting it as the full count is fabrication.
- A ZERO RESULT IS A REAL FINDING. `total_buckets: 0` on a well-targeted query is evidence
  (it rules a hypothesis out) and MUST be reported — often it is the most informative result
  you have. Never drop a query because it came back empty.
- `NO_DATA_WARNING` from get_datadog_metric is the same kind of finding: the query RAN and
  the metric has no points in range. Report it as "no data points for <metric>", never as a
  tool error and never as "the tool failed".
- `AUTO_MODIFIERS` tells you the server rewrote your metric query (e.g. `.as_count()` applied
  automatically for a count-type metric). Report the modifier with the number — it changes
  what the number means.
- Extract error-rate spikes, slow endpoints, host CPU/memory, monitor states, error spans — with
  numbers/timestamps. Cross-reference the alert time. Label STRONG EVIDENCE / MODERATE EVIDENCE /
  WEAK SIGNAL / UNKNOWN. No fabrication. Under 500 words.
- SOURCE LINKS: for every finding, include the clickable Datadog URL so the report can cite it:
  monitor -> https://us5.datadoghq.com/monitors/<id> (use the monitor id from the result);
  dashboard -> https://us5.datadoghq.com/dashboard/<id>; log query -> https://us5.datadoghq.com/logs?query=<the query you ran>.
  Emit the id AND the built URL next to each finding. If a result has no id, say so — never invent one.
- RELEVANCE GATE: only report a service/host as evidence if it is the alert's subject or a
  named dependency in {service_context}. A HEALTHY result from an unrelated service (e.g. a 200
  /api/health/live on a service the alert never names) is NOT evidence — omit it. For a
  pipeline/no-run alert with no affected service, "dev environment is healthy" is irrelevant:
  absence of the pipeline's own telemetry is the only Datadog signal that matters.
"""

datadog_tierb = LlmAgent(
    model=create_fast_model(),
    name="datadog_observability",  # keep name so synthesis/report/telemetry are unchanged
    description="Investigates Datadog via a single concurrent batch fetch (Tier B).",
    instruction=INSTRUCTION,
    output_key="findings_datadog",
    tools=[fetch_datadog_tool],
    on_tool_error_callback=keep_running_on_tool_error,
)

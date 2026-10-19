from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import (
    build_tool_list,
    create_bifrost_toolset,
    truncate_tool_response,
)
from ...tools.tool_resilience import keep_running_on_tool_error

EXTRA_CONTEXT = """
CRITICAL — Timestamp Anchoring (the window parameters are `from` and `to`):
Compute the query window from {{classification}}.alert_timestamp (ISO 8601 UTC):
- Window: (alert_timestamp - 30 min) to (alert_timestamp + 15 min).
- If alert_timestamp is "unknown", fall back to the last 1 hour — do NOT widen further.

EVERY time-scoped Datadog tool accepts a window, and the parameters are `from` and `to`
(ISO 8601, or relative like 'now-1h') — verified against the DECLARED SIGNATURES in
servers/datadog_token.pyi (readToolFile, 2026-07-28): aggregate_spans, search_datadog_spans,
search_datadog_error_tracking_issues, search_datadog_logs, analyze_datadog_logs,
search_datadog_events, get_datadog_metric all declare `from: str = None, to: str = None`.
NONE of them declares start/end. search_datadog_monitors is the only one with no window.

`from` is a RESERVED WORD in Python/Starlark, so `from="..."` is a SYNTAX ERROR. Build an
args dict and unpack it:
  args = {"query": "service:acme.example", "computes": [{"aggregation": "count"}],
          "from": "2026-07-27T13:00:00Z", "to": "2026-07-27T14:00:00Z"}
  result = datadog_token.aggregate_spans(**args)
aggregate_spans REQUIRES computes= (a list, no default in the signature) and has NO limit=
parameter — cap the response with max_tokens= instead.

NEVER use start= / end= / from_ts= / to_ts=. No Datadog tool declares those parameters. On
the span tools the server ACCEPTS the call and SILENTLY DISCARDS them, returning last-hour
data that looks windowed. Measured on one fixed aggregate_spans query: no window 514000,
start=/end= 513995, from/to 467682, no window again 514094 — only from/to moved the window.
A start=/end= call is worse than passing nothing, because it hides the problem.
An in-query @timestamp:[X TO Y] range does NOT work either; the server silently drops it.

search_datadog_monitors and search_datadog_services have NO window parameter (they report
current state, not a time series) — call those without bounds.

search_datadog_spans usually returns is_truncated=true with a truncation_message. A count
from a truncated response is a LOWER BOUND — write it "at least N (truncated)", never as a
complete total.

CRITICAL — analyze_datadog_logs is the PREFERRED tool for any LOG NUMBER, and it takes
sql_query= (NOT query=) plus from/to:
  args = {"sql_query": "SELECT status, count(*) AS n FROM logs GROUP BY status ORDER BY count(*) DESC",
          "filter": "service:acme.example",
          "from": "2026-07-27T13:00:00Z", "to": "2026-07-27T14:00:00Z"}
  result = datadog_token.analyze_datadog_logs(**args)
It aggregates server-side and returns EXACT counts for the incident window; search_datadog_logs
returns a truncated sample you would have to count by hand. Use search_datadog_logs only when
you need the raw message TEXT of individual lines. Default columns: timestamp, host, service,
env, version, status, message; the optional `filter` takes Datadog search syntax.
Do NOT put the window in the SQL: `WHERE timestamp >= TIMESTAMP '...'` parses but only narrows
WITHIN the tool's default hour — it cannot reach an older incident (measured: a 7-day predicate
returned 0 rows while the same query unfiltered returned 995,549). `from_iso8601_timestamp()`
is NOT a recognised function (400 error), and a bare ISO string fails with
"cannot apply operator: timestamp <= varchar". Use from/to.
DDSQL is a PostgreSQL subset: every non-aggregated SELECT column must appear in GROUP BY, and
aliases cannot be reused — repeat the expression (GROUP BY DATE_TRUNC('hour', timestamp)).

CRITICAL — get_datadog_metric takes queries= (a LIST), NOT query= (a string):
  args = {"queries": ['avg:trace.job.scheduled.errors{env:prod}'], "from": "<from>", "to": "<to>"}
  result = datadog_token.get_datadog_metric(**args)
query= is rejected ("unexpected additional properties"); omitting it reports
'required: missing properties: ["queries"]'. This one wrong kwarg name explains every
historical failure of this tool. Two response fields are FINDINGS, not errors:
- AUTO_MODIFIERS: the server may auto-apply .as_count() for count-type metrics. Report the
  modifier next to the number — it changes what the number means.
- NO_DATA_WARNING: the query ran and legitimately has no points in range. Report it as
  "no data points", never as a tool failure.

CRITICAL — Resolve the Datadog service name DYNAMICALLY (do NOT assume the service tag equals the app name):
Datadog service tags rarely match the human app name (e.g. "WebCore" is service:acme.example, not service:WebCore).
Discover the real tag instead of guessing:
1. Derive candidate terms from {{classification}}.affected_service, {{service_context}}, {{classification}}.keywords,
   and any resource/host names in the alert text.
2. Call search_datadog_services with those terms (via executeToolCode) to list matching services and their exact
   service tags. Pick the closest match to the alert; if several match, prefer the one whose environment matches
   {{classification}}.environment.
3. Use that discovered service tag verbatim (service:<tag>) for every search_datadog_logs / aggregate_spans /
   get_datadog_metric call below.
4. If search_datadog_services returns nothing usable, fall back to these known hints, then to a broad
   (service-unscoped) search within the query window:
   - WebCore → service:acme.example    - FileService → service:files.acme.example
   - SubscriptionService → service:subscriptionservice    - Pricing → service:app.catalog.example
Never invent a service tag you have not seen in the alert or in search_datadog_services output.

Environment-Specific SRE Dashboards (query this FIRST for top-level signal):
- Production: aaa-111-bbb
- Sandbox: ggg-444-hhh
- Test: eee-333-fff
- Ephemeral: iii-555-jjj

CRITICAL — Category → Domain Dashboard Map (pull ONLY the dashboard for {{classification}}.category —
do NOT pull every domain dashboard and filter afterward):
- sql-database, scheduled-jobs, data-integrity → SQL Server: ccc-222-ddd
- pubsub-messaging → PubSub: ooo-888-ppp
- performance → Ben/Tom Performance: mmm-777-nnn
- auth-security → GCP Load Balancer: qqq-999-rrr
- cloud-infrastructure → check {{classification}}.keywords/error_message first: "redis" → Redis: sss-010-ttt;
  "valkey" → Valkey: kkk-666-lll; otherwise GCP Load Balancer: qqq-999-rrr
- application-errors, business-logic, deploy-release, monitoring-meta, unknown → no domain dashboard;
  rely on the SRE dashboard plus service-scoped search_datadog_logs/aggregate_spans/get_datadog_metric only

IMPORTANT: Always use precise filters on aggregate_spans. Never pull all monitors — filter by alert/warn state only.

CRITICAL — limit= is not a parameter of these tools. search_datadog_monitors REJECTS limit
("unexpected additional properties [limit]" — 26 rejections observed); scope it with query=
alone. Cap response size with max_tokens= instead, and page with start_at= (a pagination
cursor — NOT a time bound, despite the name).
"""

_bifrost = create_bifrost_toolset()

_bifrost_targets = ["search_datadog_services", "search_datadog_monitors", "search_datadog_logs", "analyze_datadog_logs", "aggregate_spans", "get_datadog_dashboard", "get_datadog_metric"]

INSTRUCTION = build_agent_instruction(
    role="Datadog Observability Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. Compute the query window from {{classification}}.alert_timestamp per the Timestamp Anchoring rule above "
        "(30 min before to 15 min after; last 1 hour if unknown). Pass it as `from`/`to` via dict-unpacking on EVERY "
        "time-scoped call — aggregate_spans, search_datadog_spans, search_datadog_error_tracking_issues, "
        "search_datadog_logs, analyze_datadog_logs, search_datadog_events and get_datadog_metric all accept it. "
        "Never use start=/end= (silently dropped). Only search_datadog_monitors and search_datadog_services take no "
        "window.",
        "2. Pull the environment-specific SRE dashboard using get_datadog_dashboard (map environment from <classification> to dashboard ID above)",
        "3. Pull the ONE domain dashboard matching {{classification}}.category using the Category → Domain Dashboard map above — skip this step if the category has no domain dashboard",
        "4. Query search_datadog_monitors filtered to alert/warn state only — do NOT list every monitor in the org. "
        "Scope it with query= alone; this tool rejects limit=.",
        "5. Resolve the Datadog service tag dynamically: call search_datadog_services with terms from "
        "{{classification}}.affected_service / {{service_context}} / keywords, pick the best match, and use its exact "
        "service tag for the queries below (fall back to the known hints only if discovery returns nothing).",
        "6. For LOG EVIDENCE use analyze_datadog_logs with sql_query= plus from/to — it is the preferred tool for any "
        "log count, rate, or breakdown, because it aggregates server-side over the exact incident window (e.g. "
        "SELECT status, count(*) AS n FROM logs GROUP BY status). Scope the service with filter='service:<tag>'. "
        "Fall back to search_datadog_logs (query= plus from/to) only when you need the raw message TEXT of "
        "individual log lines.",
        "7. Use aggregate_spans with precise filters (service, resource_name, duration, status) scoped to the query "
        "window via from/to. This is your primary source of incident-window span evidence. "
        "Add search_datadog_error_tracking_issues (same from/to) for grouped errors in the window — it returns "
        "total_count and a citable issue URL — and search_datadog_spans for individual spans, reporting its counts "
        "as 'at least N (truncated)' whenever is_truncated=true.",
        "8. Query get_datadog_metric for the category's key metric(s) using queries=['<metric query>'] — "
        "a LIST, not query= — with from/to in the same args dict. "
        "Report NO_DATA_WARNING as 'no data points' and surface any AUTO_MODIFIERS next to the number.",
        "9. Extract key signals: elevated CPU/memory on specific hosts, error rate spikes, slow endpoints, trace spans with errors",
        "10. Cross-reference findings with the alert time — did metrics spike at the same time?",
    ],
    tools_available=[],
    extra_context=EXTRA_CONTEXT,
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
)

datadog_observability_agent = LlmAgent(
    model=create_fast_model(),
    name="datadog_observability",
    description="Investigates Datadog dashboards, monitors, APM traces, and metrics for correlated signals.",
    instruction=INSTRUCTION,
    output_key="findings_datadog",
    tools=build_tool_list(_bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)

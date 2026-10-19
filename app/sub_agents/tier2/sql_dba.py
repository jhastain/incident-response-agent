"""SQL Database Investigation Agent — Tier 2 conditional agent.

Dispatched when category is 'sql_database', 'performance' with DB signals, or
'scheduled_jobs' (many touch SQL). This is the MOST THOROUGH Tier 2 agent.
"""

from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import (
    build_tool_list,
    create_bifrost_toolset,
    truncate_tool_response,
)
from ...tools.gcp_tools import query_gcp_logs
from ...tools.tool_resilience import keep_running_on_tool_error

CHECKLIST = [
    "Check Datadog monitors for SQL-related alerts — filter for 'sql' or 'database' in monitor name",
    "Query slow query logs via search_datadog_logs — look for queries exceeding 5s threshold, filter by service:sqldb.acme.example duration:>5s",
    "Check for blocking sessions or lock contention — query sqlserver.stats.lock_waits, "
    "sqlserver.stats.procs_blocked and sqlserver.activity.blocked_connections, all filtered host:sqlprod*",
    "Query deadlock logs — search GCP logs for 'deadlock' in textPayload within alert window, and "
    "query the sqlserver.locks.deadlocks metric filtered host:sqlprod*",
    "Check connection pool metrics via get_datadog_metric — sqlserver.stats.connections filtered host:sqlprod*",
    "Look at query execution plans for high-cost queries — extract from Datadog APM trace metadata",
    "Check for table lock contention — look for LCK_M_IX, LCK_M_U wait states in Datadog metrics",
    "Monitor I/O wait metrics — query sqlserver.files.io_stall filtered host:sqlprod* and system.io.await",
    "Check for long-running transactions (> 30s) — filter Datadog traces by duration:>30s AND transaction active",
]

_bifrost = create_bifrost_toolset()

_bifrost_targets = ["search_datadog_monitors", "get_datadog_metric", "search_datadog_logs"]

INSTRUCTION = build_agent_instruction(
    role="SQL Database Investigation Agent",
    context_refs=["{classification}", "{synthesis}"],
    checklist=CHECKLIST,
    tools_available=["query_gcp_logs"],
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
    extra_context="""\
## SQL DBA-Specific Rules

### Dual Ranking Approach
A query running 100ms but called 1M times/hour may be worse than a single 30s query.
Rank findings by BOTH:
1. **Duration outliers**: max single execution time (blocking indicator)
2. **Volume outliers**: total_hits × avg_duration (CPU exhaustion indicator)

Present BOTH rankings side by side in your findings.

### Key Datadog Metrics (each verified live 2026-07-28 to return data)
Pass these to get_datadog_metric as a LIST: get_datadog_metric(queries=["avg:...", "avg:..."]).
- `avg:sqlserver.stats.connections{host:sqlprod*}` grouped by host — connection count per SQL host
- `avg:sqlserver.stats.lock_waits{host:sqlprod*}` — lock wait rate
- `avg:sqlserver.stats.procs_blocked{host:sqlprod*}` — processes currently blocked (blocking chain size)
- `avg:sqlserver.activity.blocked_connections{host:sqlprod*}` — blocked connection count
- `sum:sqlserver.locks.deadlocks{host:sqlprod*}` — deadlock rate
- `avg:sqlserver.files.io_stall{host:sqlprod*}` — I/O stall time (disk pressure)
- `avg:system.io.await{*}` — host-level I/O wait
- `avg:trace.sql_server.query.hits{env:prod,service:sqldb.acme.example}` grouped by resource_name — query hit count
- `avg:trace.sql_server.query.duration{env:prod,service:sqldb.acme.example}` grouped by resource_name — latency per query

### Metrics that DO NOT exist here — never query them, never report on them
Verified live 2026-07-28: these return NO_DATA in every form and are NOT in the metric catalog.
`trace.sql_server.query.duration.95p` (no .95p suffix exists — use `trace.sql_server.query.duration`),
`sqlserver.io.disk_time`, `sqlserver.replication.lag_seconds`. SQL Server replication lag is not
emitted to Datadog in this org — if the alert concerns replication, state that replication lag is
NOT OBSERVABLE via the tools you have. Do not substitute a different metric and call it lag.

### A ZERO OR EMPTY RESULT IS A REAL FINDING — NEVER A CLEAN BILL OF HEALTH
An empty metric result means the metric + tag filter matched nothing. That is NOT evidence the
database is healthy — it usually means the query was wrong.
- If a metric query returns `<NO_DATA_WARNING>` or an empty `<JSON_DATA>` block, report it as:
  "queried <exact metric+filter>, got no data — cannot conclude health" and label it UNKNOWN.
- NEVER write "no blocking detected", "no deadlocks", "connections normal", or any equivalent
  all-clear on the strength of an empty result. Only a result containing actual numeric values can
  support a statement about database health.
- A metric that returns real values at or near zero (e.g. procs_blocked max=0 over the window) IS
  a valid negative finding — quote the count/min/max and say so. Distinguish this from no data.
- If the filter may be wrong, re-query the same metric with `{*}` (no filter). If `{*}` returns data
  and your filtered query did not, the FILTER was wrong — report that, do not report health.

### Query Structure Red Flags
When you extract SQL text from traces, look for:
- Missing JOIN on key column (table scan instead of seek)
- Implicit conversion (index not used)
- UNION instead of UNION ALL (unnecessary sort/distinct)
- Missing index hint on large dimension tables
- N+1 pattern (same query thousands of times)
- Cross-shard fan-out (query hitting multiple SQL hosts)
- Filtering in WHERE vs JOIN (produces full cross product)

### Dashboard References
- SQL Server Dashboard: `ccc-222-ddd`
- Ben/Tom Performance Dashboard: `mmm-777-nnn` (the one DBAs actually use)
- SRE Prod Dashboard: `aaa-111-bbb`

Use get_datadog_dashboard to pull these and cross-reference your findings.
""",
)

sql_dba_agent = LlmAgent(
    model=create_fast_model(),
    name="sql_dba_agent",
    description="Expert SQL Server DBA agent — investigates blocking, CPU spikes, slow queries, deadlocks, and query structure issues.",
    instruction=INSTRUCTION,
    output_key="findings_sql",
    tools=build_tool_list(query_gcp_logs, _bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)

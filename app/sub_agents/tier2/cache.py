"""Cache (Redis/Valkey) Investigation Agent — Tier 2 conditional agent.

Dispatched when category is 'cloud_infrastructure' with cache signals OR
'performance' category with cache-related indicators in synthesis.
"""

from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import (
    build_tool_list,
    create_bifrost_toolset,
    truncate_tool_response,
)
from ...tools.tool_resilience import keep_running_on_tool_error

CHECKLIST = [
    "Check Datadog monitors for Redis/Valkey alert state — use search_datadog_monitors(query='redis') "
    "then search_datadog_monitors(query='valkey'). This tool takes NO limit parameter",
    "Query cache hit rate via get_datadog_metric — avg:gcp.redis.stats.cache_hit_ratio",
    "Check memory usage — avg:gcp.redis.stats.memory.usage_ratio (fraction 0-1) and "
    "avg:gcp.memorystore.instance.memory.average_utilization",
    "Check eviction rate — avg:gcp.redis.stats.evicted_keys",
    "Look for connection pool exhaustion — avg:gcp.redis.clients.connected vs avg:gcp.redis.clients.blocked",
    "Check CPU pressure — avg:gcp.redis.stats.cpu_utilization and avg:gcp.memorystore.instance.cpu.average_utilization",
    "Check cache miss patterns via search_datadog_logs — identify hot keys causing excessive misses",
]

_bifrost = create_bifrost_toolset()

_bifrost_targets = ["search_datadog_monitors", "get_datadog_metric"]

INSTRUCTION = build_agent_instruction(
    role="Cache Investigation Agent",
    context_refs=["{classification}", "{synthesis}"],
    checklist=CHECKLIST,
    tools_available=[],
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
    extra_context="""\
## Cache-Specific Rules
- Eviction rate > 100/sec indicates memory pressure — flag for capacity review
- Hit rate < 80% is abnormal for WebCore cache patterns — investigate key TTLs
- Blocked clients > 5 indicates connection pool exhaustion — check max connections config

## Metric namespace — these are Memorystore for VALKEY, surfaced through the GCP integration
Verified live 2026-07-28. Pass metrics to get_datadog_metric as a LIST:
get_datadog_metric(queries=["avg:gcp.redis.stats.cache_hit_ratio{*}", "..."]).

Metrics that RETURN DATA:
- `avg:gcp.redis.stats.cache_hit_ratio{...}` — hit ratio (fraction 0-1)
- `avg:gcp.redis.stats.memory.usage_ratio{...}` — memory used / maxmemory (fraction 0-1)
- `avg:gcp.redis.stats.memory.usage{...}` and `avg:gcp.redis.stats.memory.maxmemory{...}` — raw bytes
- `avg:gcp.redis.stats.evicted_keys{...}` — evicted keys
- `avg:gcp.redis.clients.connected{...}` / `avg:gcp.redis.clients.blocked{...}` — client connections
- `avg:gcp.redis.stats.cpu_utilization{...}` — CPU
- `avg:gcp.memorystore.instance.memory.average_utilization{...}` — Memorystore memory utilization
- `avg:gcp.memorystore.instance.cpu.average_utilization{...}` — Memorystore CPU utilization

Metrics that DO NOT EXIST here — never query them, never report on them. Verified live: all return
NO_DATA and are absent from the metric catalog:
`redis.mem.used`, `redis.maxmemory`, `redis.mem.maxmemory`, `redis.net.clients.connected`,
`redis.net.clients.blocked`, and any other bare `redis.*` name. The bare `redis.*` namespace is the
self-hosted Redis integration, which is NOT deployed here.
There is also NO fragmentation_ratio metric available — do not report on memory fragmentation.

Valid tag keys on gcp.redis.*: instance_id, node_id, project_id, region, role, cache.
Real instance ids look like
`projects/acme-production-100000/locations/us-central1/instances/WebCore-prod-redis-sessionstate-us-c1-1c38`
— a short name like "sessionstate" matches NOTHING. If unsure of the exact id, query with `{*}` first;
the returned scope/tags reveal the real ids, then re-query filtered.

## A ZERO OR EMPTY RESULT IS A REAL FINDING — NEVER A CLEAN BILL OF HEALTH
An empty metric result means the metric + tag filter matched no instance. That is NOT evidence the
cache is healthy.
- If a query returns `<NO_DATA_WARNING>` or an empty `<JSON_DATA>` block, report it as:
  "queried <exact metric+filter>, got no data — cannot conclude cache health" and label it UNKNOWN.
- NEVER write "no evictions", "memory normal", "hit rate healthy", or any equivalent all-clear on
  the strength of an empty result. Only a result containing actual numeric values supports a
  statement about cache health.
- A metric returning real values at or near zero (e.g. clients.blocked all 0 across the window) IS a
  valid negative finding — quote the values and say so. Distinguish this from no data.
- If your filtered query is empty, re-query the same metric with `{*}`. If `{*}` returns data, the
  FILTER was wrong — report that, do not report health.
""",
)

cache_agent = LlmAgent(
    model=create_fast_model(),
    name="cache_agent",
    description="Investigates Redis/Valkey cache health, memory usage, eviction patterns, and connection pool status.",
    instruction=INSTRUCTION,
    output_key="findings_cache",
    tools=build_tool_list(_bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)

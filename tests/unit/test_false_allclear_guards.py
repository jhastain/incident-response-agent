"""Regression tests for the FALSE ALL-CLEAR defects (verified live 2026-07-28 via Bifrost).

A crash is visible. A false all-clear is not. Each defect below made an agent report
"nothing wrong here" when it had never actually looked:

Defect 1: sql_dba.py named metrics that emit nothing (trace.sql_server.query.duration.95p,
          sqlserver.io.disk_time, sqlserver.replication.lag_seconds) and had no no-data
          guard, so NO_DATA read as "no blocking detected".
Defect 2: cache.py queried the bare `redis.*` namespace (self-hosted Redis integration,
          not deployed here). Every query returned NO_DATA, which read as a healthy cache.
          The live namespace is gcp.redis.* / gcp.memorystore.* (Memorystore for Valkey via
          the GCP integration).
Defect 3: sentry_error.py / sentry_tierb.py named five project slugs. Three do not exist —
          they return HTTP 404 "Project does not exist", which read as "no Sentry errors".
Defect 4: get_sentry_resource was called with issueId and no resourceType (Input Error), and
          find_releases with projectSlugOrId, which is SILENTLY IGNORED and returns org-wide
          releases — wrong-project release correlation presented as fact.
Defect 5: known_issues*.py instructed an ad-hoc WIQL query. There is no WIQL execution path
          on this VK; the call returns literal `null`, which read as "no known issues".
Defect 6: shared_prompts.py mandated `top=5 or limit=5` on EVERY search/list/query call.
          search_datadog_monitors and search_datadog_logs REJECT those kwargs outright
          (`unexpected additional properties ["limit"]`), so the blanket rule turned working
          calls into hard failures.

Live evidence quoted in each test's assertion message where it is load-bearing.
"""

import re

from app.shared_prompts import TOKEN_BUDGET_RULES
from app.sub_agents.tier1.known_issues import INSTRUCTION as KNOWN_ISSUES_TIER_A
from app.sub_agents.tier1.known_issues_tierb import INSTRUCTION as KNOWN_ISSUES_TIER_B
from app.sub_agents.tier1.sentry_error import INSTRUCTION as SENTRY_TIER_A
from app.sub_agents.tier1.sentry_tierb import INSTRUCTION as SENTRY_TIER_B
from app.sub_agents.tier2.cache import CHECKLIST as CACHE_CHECKLIST
from app.sub_agents.tier2.cache import INSTRUCTION as CACHE_INSTRUCTION
from app.sub_agents.tier2.sql_dba import CHECKLIST as SQL_CHECKLIST
from app.sub_agents.tier2.sql_dba import INSTRUCTION as SQL_INSTRUCTION

# ---------------------------------------------------------------------------
# Live-verified fact tables. Every name below was probed against Bifrost on
# 2026-07-28; "dead" means the server answered <NO_DATA_WARNING> or 404.
# ---------------------------------------------------------------------------

# Returned real values (count/min/max present in overall_stats or a values series).
LIVE_SQL_METRICS = [
    "sqlserver.stats.connections",
    "sqlserver.stats.lock_waits",
    "sqlserver.stats.procs_blocked",
    "sqlserver.activity.blocked_connections",
    "sqlserver.locks.deadlocks",
    "sqlserver.files.io_stall",
    "system.io.await",
    "trace.sql_server.query.hits",
    "trace.sql_server.query.duration",
]
# Returned <NO_DATA_WARNING> and are absent from search_datadog_metrics(name_filter=...).
DEAD_SQL_METRICS = [
    "trace.sql_server.query.duration.95p",
    "sqlserver.io.disk_time",
    "sqlserver.replication.lag_seconds",
]

LIVE_CACHE_METRICS = [
    "gcp.redis.stats.cache_hit_ratio",
    "gcp.redis.stats.memory.usage_ratio",
    "gcp.redis.stats.evicted_keys",
    "gcp.redis.clients.connected",
    "gcp.redis.clients.blocked",
    "gcp.redis.stats.cpu_utilization",
    "gcp.memorystore.instance.memory.average_utilization",
    "gcp.memorystore.instance.cpu.average_utilization",
]
DEAD_CACHE_METRICS = [
    "redis.mem.used",
    "redis.maxmemory",
    "redis.net.clients.connected",
    "redis.net.clients.blocked",
]

# find_projects(organizationSlug="acme") on 2026-07-28.
REAL_SENTRY_SLUGS = ["webcore-dotnet", "webcore-react", "subscription-service", "api-v2"]
# search_issues with each of these: HTTP 404 "Project does not exist".
FAKE_SENTRY_SLUGS = ["acme-net", "webcore-frontend", "subscriptionservice", "pricing"]

# The complete azure_devops_token surface, from dir(azure_devops_token).
ADO_TOOL_SURFACE = {
    "core_get_identity_ids",
    "repo_get_branch_by_name",
    "repo_get_pull_request_by_id",
    "repo_get_repo_by_name_or_id",
    "wit_get_query",
    "wit_get_query_results_by_id",
    "wit_get_work_item",
    "wit_get_work_item_type",
    "wit_get_work_items_batch_by_ids",
    "wit_get_work_items_for_iteration",
}


def _mentions(haystack: str, needle: str) -> bool:
    """Whole-token match so 'redis.mem.used' does not match inside a longer name."""
    return re.search(rf"(?<![\w.]){re.escape(needle)}(?![\w.])", haystack) is not None


# ---------------------------------------------------------------------------
# Defect 1 — sql_dba.py: dead metrics dropped, no-data guard present
# ---------------------------------------------------------------------------


def test_sql_dba_names_only_metrics_that_emit_data():
    for metric in LIVE_SQL_METRICS:
        assert _mentions(SQL_INSTRUCTION, metric), (
            f"{metric} returned live data on 2026-07-28 but sql_dba.py no longer names it"
        )


def test_sql_dba_does_not_direct_queries_at_dead_metrics():
    """Dead names may appear ONLY inside the do-not-query warning block."""
    warning_start = SQL_INSTRUCTION.index("### Metrics that DO NOT exist here")
    warning_end = SQL_INSTRUCTION.index("### A ZERO OR EMPTY RESULT IS A REAL FINDING")
    outside = SQL_INSTRUCTION[:warning_start] + SQL_INSTRUCTION[warning_end:]
    for metric in DEAD_SQL_METRICS:
        assert not _mentions(outside, metric), (
            f"{metric} returned <NO_DATA_WARNING> live; it must not be presented as a "
            f"metric to query — only inside the do-not-query warning"
        )


def test_sql_dba_checklist_has_no_dead_metric_steps():
    joined = "\n".join(SQL_CHECKLIST)
    for metric in DEAD_SQL_METRICS:
        assert not _mentions(joined, metric), (
            f"checklist step still aims at dead metric {metric}"
        )


def test_sql_dba_has_explicit_no_data_guard():
    assert "A ZERO OR EMPTY RESULT IS A REAL FINDING" in SQL_INSTRUCTION
    assert "NEVER A CLEAN BILL OF HEALTH" in SQL_INSTRUCTION
    assert "cannot conclude health" in SQL_INSTRUCTION


def test_sql_dba_forbids_the_specific_false_allclear_phrases():
    for phrase in ["no blocking detected", "no deadlocks", "connections normal"]:
        assert phrase in SQL_INSTRUCTION, (
            f"the guard must name '{phrase}' as forbidden — a generic warning did not stop "
            f"this in production"
        )


def test_sql_dba_distinguishes_real_zero_from_no_data():
    assert "Distinguish this from no data" in SQL_INSTRUCTION


def test_sql_dba_says_queries_is_a_list():
    assert "queries=[" in SQL_INSTRUCTION


def test_sql_dba_negative_documents_replication_lag():
    assert "NOT OBSERVABLE" in SQL_INSTRUCTION


# ---------------------------------------------------------------------------
# Defect 2 — cache.py: correct namespace, no-data guard
# ---------------------------------------------------------------------------


def test_cache_names_only_metrics_that_emit_data():
    for metric in LIVE_CACHE_METRICS:
        assert _mentions(CACHE_INSTRUCTION, metric), (
            f"{metric} returned live data on 2026-07-28 but cache.py does not name it"
        )


def test_cache_does_not_direct_queries_at_the_dead_redis_namespace():
    warning_start = CACHE_INSTRUCTION.index("Metrics that DO NOT EXIST here")
    warning_end = CACHE_INSTRUCTION.index("Valid tag keys on gcp.redis.*")
    outside = CACHE_INSTRUCTION[:warning_start] + CACHE_INSTRUCTION[warning_end:]
    for metric in DEAD_CACHE_METRICS:
        assert not _mentions(outside, metric), (
            f"{metric} returned <NO_DATA_WARNING> live (bare redis.* is the self-hosted "
            f"integration, not deployed here); it must not be presented as queryable"
        )


def test_cache_checklist_uses_the_live_namespace():
    joined = "\n".join(CACHE_CHECKLIST)
    for metric in DEAD_CACHE_METRICS:
        assert not _mentions(joined, metric), f"checklist step still aims at dead metric {metric}"
    assert "gcp.redis." in joined


def test_cache_has_explicit_no_data_guard():
    assert "A ZERO OR EMPTY RESULT IS A REAL FINDING" in CACHE_INSTRUCTION
    assert "NEVER A CLEAN BILL OF HEALTH" in CACHE_INSTRUCTION
    assert "cannot conclude cache health" in CACHE_INSTRUCTION


def test_cache_forbids_the_specific_false_allclear_phrases():
    for phrase in ["no evictions", "memory normal", "hit rate healthy"]:
        assert phrase in CACHE_INSTRUCTION, f"the guard must name '{phrase}' as forbidden"


def test_cache_drops_the_unavailable_fragmentation_metric():
    """No fragmentation_ratio metric exists; the old prompt set a 1.5 threshold on it."""
    assert "fragmentation_ratio > 1.5" not in CACHE_INSTRUCTION
    assert "do not report on memory fragmentation" in CACHE_INSTRUCTION


def test_cache_warns_that_short_instance_names_match_nothing():
    assert "matches NOTHING" in CACHE_INSTRUCTION


# ---------------------------------------------------------------------------
# Defect 3 — Sentry project slugs
# ---------------------------------------------------------------------------


def test_sentry_prompts_name_the_real_slugs():
    for prompt_name, prompt in (("tier A", SENTRY_TIER_A), ("tier B", SENTRY_TIER_B)):
        for slug in REAL_SENTRY_SLUGS:
            assert _mentions(prompt, slug), f"{prompt_name} is missing real slug {slug}"


def test_sentry_prompts_do_not_route_investigations_to_404_slugs():
    """Fake slugs may appear ONLY in the do-not-use warning."""
    for prompt_name, prompt, marker in (
        ("tier A", SENTRY_TIER_A, "These slugs DO NOT EXIST"),
        ("tier B", SENTRY_TIER_B, "These slugs DO NOT EXIST"),
    ):
        warn_idx = prompt.index(marker)
        before = prompt[:warn_idx]
        for slug in FAKE_SENTRY_SLUGS:
            assert not _mentions(before, slug), (
                f"{prompt_name} still maps a service to {slug!r}, which returns HTTP 404 "
                f'"Project does not exist" — an empty result that reads as "no Sentry errors"'
            )


def test_sentry_prompts_explain_404_is_not_a_finding():
    for prompt in (SENTRY_TIER_A, SENTRY_TIER_B):
        assert "404" in prompt
        assert "Project does not exist" in prompt


def test_sentry_tierb_separates_404_from_a_genuine_empty_result():
    assert "the slug was WRONG" in SENTRY_TIER_B
    assert "a genuine negative finding" in SENTRY_TIER_B


# ---------------------------------------------------------------------------
# Defect 4 — get_sentry_resource / find_releases call forms
# ---------------------------------------------------------------------------


def test_get_sentry_resource_uses_resource_type_enum_not_issue_id():
    for prompt_name, prompt in (("tier A", SENTRY_TIER_A), ("tier B", SENTRY_TIER_B)):
        assert 'resourceType="issue"' in prompt or "resourceType='issue'" in prompt, (
            f"{prompt_name}: resourceType is required — omitting it returns "
            f'"Either `url` or `resourceType` must be provided"'
        )
        assert "issueId=" not in prompt, f"{prompt_name}: there is no issueId parameter"


def test_get_sentry_resource_documents_the_enum_values():
    assert "issue | event | trace | span | breadcrumbs | replay" in SENTRY_TIER_A
    assert "issue | event | trace | span | breadcrumbs |" in SENTRY_TIER_B


def test_find_releases_uses_project_slug_not_project_slug_or_id():
    """projectSlugOrId is silently dropped: the response header changes from
    'Releases in acme/webcore-dotnet' to 'Releases in acme' (all projects)."""
    for prompt_name, prompt in (("tier A", SENTRY_TIER_A), ("tier B", SENTRY_TIER_B)):
        assert "find_releases(projectSlug=" in prompt or 'find_releases(organizationSlug="acme", projectSlug=' in prompt, (
            f"{prompt_name} must call find_releases with projectSlug"
        )
        assert "SILENTLY IGNORED" in prompt, (
            f"{prompt_name} must warn that projectSlugOrId on find_releases is dropped"
        )


def test_find_releases_warning_is_not_undercut_by_a_projectslugorid_example():
    bad = 'find_releases(organizationSlug="acme", projectSlugOrId='
    for prompt_name, prompt in (("tier A", SENTRY_TIER_A), ("tier B", SENTRY_TIER_B)):
        assert bad not in prompt, f"{prompt_name} still shows the dropped-filter call form"


# ---------------------------------------------------------------------------
# Defect 5 — known_issues WIQL path is dead; must be negative-documented
# ---------------------------------------------------------------------------


def test_known_issues_prompts_negative_document_the_missing_search():
    for prompt_name, prompt in (
        ("tier A", KNOWN_ISSUES_TIER_A),
        ("tier B", KNOWN_ISSUES_TIER_B),
    ):
        assert "Known-issues search unavailable" in prompt, (
            f"{prompt_name} must state the limitation outright, the way codebase.py does for "
            f"code search"
        )
        assert "NO ad-hoc WIQL execution path" in prompt or "no ad-hoc WIQL" in prompt


def test_known_issues_prompts_forbid_claiming_a_clean_search():
    for prompt_name, prompt in (
        ("tier A", KNOWN_ISSUES_TIER_A),
        ("tier B", KNOWN_ISSUES_TIER_B),
    ):
        assert "no known issues found" in prompt, (
            f"{prompt_name} must name the exact forbidden phrase — a WIQL call returns literal "
            f"null, and reporting that as an all-clear is the false conclusion"
        )
        assert "NEVER" in prompt


def test_known_issues_prompts_no_longer_instruct_building_a_wiql_query():
    for prompt_name, prompt in (
        ("tier A", KNOWN_ISSUES_TIER_A),
        ("tier B", KNOWN_ISSUES_TIER_B),
    ):
        assert "ONE combined WIQL query" not in prompt, (
            f"{prompt_name} still instructs constructing a WIQL query that cannot execute"
        )
        assert "OR's together" not in prompt


def test_known_issues_prompts_document_search_workitem_as_absent():
    """search_workitem is NOT on this server: '"azure_devops_token" struct has no
    .search_workitem attribute'. It may be named only to say it does not exist."""
    for prompt_name, prompt in (
        ("tier A", KNOWN_ISSUES_TIER_A),
        ("tier B", KNOWN_ISSUES_TIER_B),
    ):
        for match in re.finditer(r"search_workitem", prompt):
            window = prompt[max(0, match.start() - 120) : match.end() + 120]
            assert "DOES NOT EXIST" in window or "NOT on this server" in window, (
                f"{prompt_name} mentions search_workitem outside a does-not-exist warning"
            )


def test_known_issues_prompts_only_reference_ado_tools_that_exist():
    for prompt_name, prompt in (
        ("tier A", KNOWN_ISSUES_TIER_A),
        ("tier B", KNOWN_ISSUES_TIER_B),
    ):
        for tool in re.findall(r"\b(wit_[a-z_]+|repo_[a-z_]+|core_[a-z_]+)\b", prompt):
            assert tool in ADO_TOOL_SURFACE, (
                f"{prompt_name} references {tool}, absent from the live ADO tool surface"
            )


def test_known_issues_keeps_the_working_id_based_path():
    for prompt in (KNOWN_ISSUES_TIER_A, KNOWN_ISSUES_TIER_B):
        assert "wit_get_work_items_batch_by_ids" in prompt
        assert "saved-query GUID" in prompt


def test_known_issues_tier_a_bifrost_targets_are_live_tools():
    from app.sub_agents.tier1 import known_issues

    if known_issues._bifrost_targets:
        for tool in known_issues._bifrost_targets:
            assert tool in ADO_TOOL_SURFACE, f"{tool} is not on the live ADO tool surface"


# ---------------------------------------------------------------------------
# Defect 6 — the blanket limit=5 rule
# ---------------------------------------------------------------------------


def test_token_budget_no_longer_mandates_a_limit_on_every_call():
    assert "Never omit the limit parameter" not in TOKEN_BUDGET_RULES, (
        "the blanket rule is injected into EVERY agent and turned working "
        "search_datadog_monitors/search_datadog_logs calls into hard failures"
    )
    assert "Use top=5 or limit=5 for all search, list, and query operations" not in TOKEN_BUDGET_RULES


def test_token_budget_names_the_tools_that_reject_the_kwarg():
    assert "search_datadog_monitors" in TOKEN_BUDGET_RULES
    assert "search_datadog_logs" in TOKEN_BUDGET_RULES
    assert 'unexpected additional properties ["limit"]' in TOKEN_BUDGET_RULES


def test_token_budget_states_an_unsupported_kwarg_is_a_hard_failure():
    assert "HARD FAILURE" in TOKEN_BUDGET_RULES
    assert "zero\n   data back" in TOKEN_BUDGET_RULES or "zero data back" in TOKEN_BUDGET_RULES


def test_token_budget_gives_a_real_alternative_to_capping():
    assert "NARROWING THE QUERY" in TOKEN_BUDGET_RULES
    assert "never by guessing a kwarg name" in TOKEN_BUDGET_RULES


def test_token_budget_still_keeps_the_call_ceiling():
    """Removing the limit rule must not remove the tool-call budget with it."""
    assert "Maximum 6 tool calls total per investigation" in TOKEN_BUDGET_RULES


def test_cache_checklist_does_not_pass_limit_to_search_datadog_monitors():
    joined = "\n".join(CACHE_CHECKLIST)
    assert "search_datadog_monitors" in joined
    assert "NO limit parameter" in joined
    assert not re.search(r"search_datadog_monitors\([^)]*limit=", joined)


def test_no_owned_prompt_passes_limit_to_a_rejecting_tool():
    rejecting = ("search_datadog_monitors", "search_datadog_logs")
    prompts = {
        "sql_dba": SQL_INSTRUCTION,
        "cache": CACHE_INSTRUCTION,
        "sentry_tier_a": SENTRY_TIER_A,
        "sentry_tier_b": SENTRY_TIER_B,
        "known_issues_tier_a": KNOWN_ISSUES_TIER_A,
        "known_issues_tier_b": KNOWN_ISSUES_TIER_B,
    }
    for name, prompt in prompts.items():
        for tool in rejecting:
            assert not re.search(rf"{tool}\([^)]*\b(limit|top|page_limit|max_results)=", prompt), (
                f"{name} passes a limit kwarg to {tool}, which rejects it with "
                f'invalid params: unexpected additional properties'
            )

"""Regression tests for the Sentry boolean-operator defect measured 2026-08-07.

Sentry's issue search rejects any query containing `OR` or `AND`:

    HTTP 400 - Error parsing search query: Boolean statements containing
    "OR" or "AND" are not supported in this search

The whole query fails, so the agent gets ZERO issues back — which is
indistinguishable from "this service has no errors". Across five live sessions on
the test engine, 24 boolean queries produced 44 rejections and no data.

Datadog's query language DOES support OR/AND, and one session sent 8 boolean
Datadog queries with 0 rejections. The fix must not touch those — see
TestDatadogIsUnaffected.
"""

import pytest

from app.shared_prompts import TOKEN_BUDGET_RULES
from app.sub_agents.tier1.sentry_tierb import INSTRUCTION
from app.tools.bifrost_direct import (
    expand_boolean_sentry_queries as _expand_boolean_queries,
)
from app.tools.bifrost_direct import (
    split_boolean_sentry_query as _split_boolean_query,
)

# Verbatim query strings sent by the deployed agent, recovered from live session
# events. Each one returned HTTP 400 and no data.
LIVE_REJECTED_QUERIES = [
    "level:error OR level:alert",
    "autoscaler OR buildserver level:error",
    "cloud-run OR autoscaler OR build OR infrastructure level:error",
    "Cloud Run OR GCP OR autoscaler OR buildserver level:error",
    "cloud-run OR GCP OR error level:error",
    "Auth0 OR JWT OR token validation level:error",
    "login OR session OR assertion level:error",
    "pipeline OR terraform OR deploy level:error",
    "level:error (startup OR initialization OR HealthCheck OR probe OR StackOverflow)",
    "level:error (Redis OR connection OR timeout) environment:test",
    "schedule OR calendar OR failed to load level:error",
    "parsing OR deserialize OR JSON level:error",
]


def _call(query: str) -> str:
    return (
        'result = sentry_token.search_issues(organizationSlug="acme", '
        f'projectSlugOrId="webcore-dotnet", query="{query}", sort="freq", limit=5)\nprint(result)'
    )


class TestLiveRejectedQueriesAreFixed:
    """Every query that actually failed in production must now be rewritten."""

    @pytest.mark.parametrize("query", LIVE_REJECTED_QUERIES)
    def test_no_boolean_survives_expansion(self, query):
        codes, notes = _expand_boolean_queries([_call(query)])

        assert notes, f"query was not rewritten: {query}"
        assert len(codes) > 1
        for code in codes:
            sent = code.split('query="', 1)[1].split('"', 1)[0]
            assert " OR " not in f" {sent} ", f"OR survived in: {sent}"
            assert " AND " not in f" {sent} ", f"AND survived in: {sent}"

    @pytest.mark.parametrize("query", LIVE_REJECTED_QUERIES)
    def test_expansion_preserves_every_search_term(self, query):
        """A rewrite that drops terms would trade a loud failure for a quiet one."""
        codes, _ = _expand_boolean_queries([_call(query)])
        rendered = " ".join(codes)

        for term in query.replace("(", " ").replace(")", " ").split():
            if term in ("OR", "AND"):
                continue
            assert term in rendered, f"term '{term}' lost from: {query}"


class TestSharedFilterIsKept:
    def test_level_filter_reattaches_to_each_part(self):
        parts = _split_boolean_query("level:error (Redis OR connection OR timeout)")

        assert len(parts) == 3
        assert all(p.startswith("level:error") for p in parts), parts
        assert {"Redis", "connection", "timeout"} == {p.split()[-1] for p in parts}

    def test_multiple_filters_reattach(self):
        parts = _split_boolean_query("level:error (Redis OR timeout) environment:test")

        for p in parts:
            assert "level:error" in p
            assert "environment:test" in p

    def test_trailing_filter_reattaches(self):
        """`a OR b level:error` — the filter trails the boolean group."""
        parts = _split_boolean_query("autoscaler OR buildserver level:error")

        assert len(parts) == 2
        assert all("level:error" in p for p in parts), parts


class TestNonBooleanIsUntouched:
    def test_plain_query_passes_through_unchanged(self):
        code = _call("AppSqlException level:error")
        codes, notes = _expand_boolean_queries([code])

        assert codes == [code]
        assert notes == []

    def test_word_containing_or_is_not_split(self):
        """ANDROID and ORDER contain the letters but are not operators."""
        code = _call("ANDROID ORDER level:error")
        codes, notes = _expand_boolean_queries([code])

        assert codes == [code]
        assert notes == []

    def test_non_search_calls_pass_through(self):
        code = (
            'result = sentry_token.get_sentry_resource(organizationSlug="acme", '
            'resourceType="issue", resourceId="WEBCORE-DOTNET-3C4D")\nprint(result)'
        )
        codes, notes = _expand_boolean_queries([code])

        assert codes == [code]
        assert notes == []


class TestDatadogIsUnaffected:
    """Datadog accepts OR/AND. Stripping them there would break working queries."""

    def test_datadog_style_query_is_not_rewritten(self):
        """This exact query ran live against Datadog with zero rejections."""
        code = (
            'result = datadog_token.search_datadog_logs('
            'query="service:acme.example (timeout OR deadlock) status:error")\nprint(result)'
        )
        codes, notes = _expand_boolean_queries([code])

        assert codes == [code], "a Datadog boolean query must not be split"
        assert notes == []


class TestTheRewriteIsReported:
    def test_note_names_the_original_and_the_replacements(self):
        _, notes = _expand_boolean_queries([_call("Redis OR timeout")])

        assert len(notes) == 1
        assert "Redis OR timeout" in notes[0]
        assert "HTTP 400" in notes[0]


class TestPromptsCarryTheRule:
    def test_sentry_prompt_forbids_boolean_operators(self):
        assert "NEVER put `OR` or `AND` in a search_issues query" in INSTRUCTION

    def test_sentry_prompt_says_datadog_is_different(self):
        assert "Datadog queries (fetch_datadog) DO support OR/AND" in INSTRUCTION

    def test_sentry_prompt_forbids_reporting_a_rejection_as_clean(self):
        assert 'Never report this as "no Sentry errors"' in INSTRUCTION

    def test_shared_rules_scope_the_restriction_to_sentry(self):
        assert "Sentry search does NOT support boolean operators" in TOKEN_BUDGET_RULES
        assert "applies to Sentry ONLY" in TOKEN_BUDGET_RULES


class TestGuardCoversEveryTierBPath:
    """The 2026-08-07 miss: the guard was scoped to fetch_sentry only.

    Four Tier-B modules call the Bifrost executor, and any of them can address any
    MCP server. In run 5 of the Platform-Alerts batch, `deploy_correlation` sent
    `sentry_token.search_issues(..., query="auth OR Auth0 OR login OR assertion")`
    through fetch_deploy and took the HTTP 400 — straight past a guard that lived in
    sentry_tierb. Hence the rewrite now sits in the executor itself.
    """

    def test_guard_lives_in_the_shared_executor(self):
        from app.tools import bifrost_direct

        assert hasattr(bifrost_direct, "expand_boolean_sentry_queries")

    def test_no_tierb_module_calls_the_unannotated_executor(self):
        """Zipping results against the REQUESTED codes misaligns after a rewrite."""
        import pathlib

        tierb = pathlib.Path("app/sub_agents/tier1").glob("*_tierb.py")
        offenders = []
        for path in tierb:
            src = path.read_text(encoding="utf-8")
            if "bifrost_execute_many(" in src:
                offenders.append(path.name)
        assert not offenders, f"must use bifrost_execute_many_annotated: {offenders}"

    def test_the_exact_query_that_slipped_through_is_now_rewritten(self):
        """Verbatim from live session, deploy_correlation, run 5."""
        code = (
            'result = sentry_token.search_issues(organizationSlug="acme", '
            'projectSlugOrId="webcore-dotnet", query="auth OR Auth0 OR login OR assertion")'
        )
        codes, notes = _expand_boolean_queries([code])

        assert notes, "the query that caused the live HTTP 400 was not rewritten"
        assert len(codes) == 4
        for c in codes:
            sent = c.split('query="', 1)[1].split('"', 1)[0]
            assert " OR " not in f" {sent} "

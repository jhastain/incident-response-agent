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
Sentry organization slug is always "acme".

## Project slugs — verified live 2026-07-28 via find_projects(organizationSlug="acme")
A WRONG SLUG RETURNS HTTP 404 "Project does not exist", NOT an empty result. Never read a 404 as
"no Sentry errors for this service" — it means you asked for a project that does not exist.

- WebCore monolith (.NET): webcore-dotnet
- WebCore frontend (React): webcore-react
- WebCore mobile: webcore-mobile
- SubscriptionService: subscription-service
- API gateway / API v2: api-v2
- Others: accounting-service, auth0-logs, platform-ai, webportal-js, webportal-php, partner-rebates,
  global-product-catalog, media-service, notifications-service, open-platform,
  payment-processing-service, payment-processing-service-js, rebate-service-dotnet,
  rebate-service-react, reference-architecture, search-service, sms-bot, sms-bot-hj,
  underwriting-service, wallet-core

These slugs DO NOT EXIST — verified 404 live. Never use them:
acme-net, webcore-frontend, fileservice, subscriptionservice, pricing.
There is no FileService or Pricing Sentry project; FileService errors surface under webcore-dotnet.
If the affected service does not map to a slug above, call
find_projects(organizationSlug="acme", query="<term>") to resolve it before searching.

## Call forms — verified live 2026-07-28
- search_issues(organizationSlug="acme", projectSlugOrId="<slug>", query="<q>", sort="freq", limit=5)
- get_sentry_resource(organizationSlug="acme", resourceType="issue", resourceId="<ISSUE-SHORT-ID>")
  resourceType is REQUIRED and is an enum: issue | event | trace | span | breadcrumbs | replay.
  resourceId is the short id from search_issues (e.g. "WEBCORE-DOTNET-3C4D"). Omitting resourceType
  returns an Input Error. There is no issueId parameter.
- find_releases(organizationSlug="acme", projectSlug="<slug>")
  The parameter is projectSlug. Passing projectSlugOrId is SILENTLY IGNORED and returns
  org-wide releases across every project — verified differentially. Never use projectSlugOrId here.

164K events/day across all projects. Filter to Production environment only.

CRITICAL: Always pass limit=5 to search_issues. Get details for only the top 3 issues, not all 5.
"""

_bifrost = create_bifrost_toolset()

_bifrost_targets = ["search_issues", "get_sentry_resource", "find_releases", "find_projects"]

INSTRUCTION = build_agent_instruction(
    role="Sentry Error Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. List recent issues for the affected Sentry project using search_issues, sorted by frequency (highest first) — set limit=5",
        "2. Get error details for the top 3 issues using "
        "get_sentry_resource(resourceType='issue', resourceId=<short id>) — extract stack traces, "
        "breadcrumbs, tags, and affected users",
        "3. Check release correlation using find_releases(projectSlug=<slug>) — did errors start appearing with a specific release? Compare issue.firstSeen vs release deployment time",
        "4. Determine error rate trend: is this a NEW error (first seen in last 24h), SPIKING (10x increase), or PRE-EXISTING?",
        "5. Extract affected endpoints and user count from error metadata (tags, request.url)",
        "6. Cross-reference error timestamps with alert time — did Sentry errors spike at the same time as the alert?",
    ],
    tools_available=[],
    extra_context=EXTRA_CONTEXT,
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
)

sentry_error_agent = LlmAgent(
    model=create_fast_model(),
    name="sentry_error",
    description="Investigates Sentry issues, error rates, release correlation, and affected users.",
    instruction=INSTRUCTION,
    output_key="findings_sentry",
    tools=build_tool_list(_bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)

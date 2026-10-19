"""Auth/Security Investigation Agent — Tier 2 conditional agent.

Dispatched when category is 'auth_security' OR Tier 1 synthesis finds auth failures/WAF events.
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
    "Check for authentication failures via search_datadog_logs — filter for HTTP 401 responses in alert window",
    "Check for authorization failures via search_datadog_logs — filter for HTTP 403 responses",
    "Look for brute force indicators via search_datadog_monitors — >10 failed auth in 5 min from same source IP",
    "Check certificate expiration dates — query GCP certificate manager for certs expiring within 30 days",
    "Review security-related audit logs via query_gcp_logs — filter for resource.type=gce_firewall_rule or cloud_armor",
    "Check for unusual access patterns or IP addresses — look for geo-location outliers in Auth0 logs",
]

_bifrost = create_bifrost_toolset()

_bifrost_targets = ["search_datadog_monitors", "search_datadog_logs"]

INSTRUCTION = build_agent_instruction(
    role="Auth/Security Investigation Agent",
    context_refs=["{classification}", "{synthesis}"],
    checklist=CHECKLIST,
    tools_available=["query_gcp_logs"],
    bifrost_target_tools=_bifrost_targets if _bifrost else None,
    extra_context="""\
## Auth/Security-Specific Rules
- 401 errors = authentication failure (who are you?)
- 403 errors = authorization failure (you're not allowed)
- Brute force threshold: >10 failed attempts from same IP in 5 minutes
- Certificate expiry warnings trigger at 30 days, critical at 7 days
- Cloud Armor blocks are visible in http_load_balancer logs with severity=WARNING
- JA3 fingerprints can identify bot/DDoS patterns — look for repeated JA3 hashes

### Key Patterns to Detect
- Auth0 error rate spike → check Auth0 dashboard via Datadog integration
- WAF blocks → check Cloud Armor policy rules for recent changes
- Certificate issues → check GCP certificate manager for expiration or renewal failures
- Unusual geo patterns → cross-reference IP addresses with known attack sources
""",
)

auth_security_agent = LlmAgent(
    model=create_fast_model(),
    name="auth_security_agent",
    description="Investigates authentication failures, authorization issues, DDoS attacks, certificate problems, and security events.",
    instruction=INSTRUCTION,
    output_key="findings_auth",
    tools=build_tool_list(query_gcp_logs, _bifrost),
    on_tool_error_callback=keep_running_on_tool_error,
    after_tool_callback=truncate_tool_response,
)

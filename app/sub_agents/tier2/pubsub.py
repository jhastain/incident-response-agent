"""Pub/Sub Investigation Agent — Tier 2 conditional agent.

Dispatched when category is 'pubsub_messaging' OR Tier 1 synthesis finds PubSub-related signals.
"""

from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import truncate_tool_response
from ...tools.gcp_tools import query_gcp_logs

CHECKLIST = [
    "Query Pub/Sub subscription logs for the affected service using query_gcp_logs with resource.type=pubsub_subscription",
    "Check for dead-letter queue messages in the last hour — filter severity=ERROR and textPayload contains 'dead letter'",
    "Check message backlog and undelivered message count via GCP Monitoring metrics",
    "Look for publish errors or acknowledge failures — filter for protoPayload.methodName contains 'Publish' or 'Acknowledge'",
    "Check subscription push endpoint health if applicable — query HTTP response codes from push endpoint logs",
    "Compare message throughput to baseline — query publish_message_operation_count and pull_message_operation_count for last 24h",
]

INSTRUCTION = build_agent_instruction(
    role="Pub/Sub Investigation Agent",
    context_refs=["{classification}", "{synthesis}"],
    checklist=CHECKLIST,
    tools_available=["query_gcp_logs", "GCP Monitoring API (via synthesis context)"],
    extra_context="""\
## Pub/Sub-Specific Rules
- Always trace the PUBLISHER, not just the consumer — unexpected publish volume is often the root cause
- Backlog size alone is not actionable — identify WHAT is publishing and WHY
- Check IAM permissions on subscriptions if consumer pull fails
- Push endpoint failures are visible in HTTP response codes — look for 4xx/5xx
- Dead-letter queues are a symptom, not a cause — find what's generating bad messages
""",
)

pubsub_agent = LlmAgent(
    model=create_fast_model(),
    name="pubsub_agent",
    description="Investigates Pub/Sub subscription backlogs, dead-letter queues, consumer failures, and message delivery issues.",
    instruction=INSTRUCTION,
    output_key="findings_pubsub",
    tools=[query_gcp_logs],
    after_tool_callback=truncate_tool_response,
)

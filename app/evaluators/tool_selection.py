"""Tool Selection judge — did the agent investigate the right things?

verification_agent grades the OUTPUT of an investigation. It cannot see whether
the right route was taken: a report can be well-evidenced and still have skipped
the one sub-agent that would have found the actual cause. This judge is the only
practical read on the "Actions & impact" pillar.

Configured in Datadog against the Bifrost connector (an AI Gateway provider,
which publish_evaluator cannot express). This file is the reviewable source of
truth for the prompt — keep the two in sync by hand when either changes.
"""

from __future__ import annotations

from ddtrace.llmobs._evaluators.llm_judge import BooleanStructuredOutput, LLMJudge

EVAL_NAME = "ir_tool_selection"

SYSTEM_PROMPT = """\
You are grading an automated incident-response investigation on ROUTE, not on
prose. You are given the alert it received and the tools it actually called.
Judge only whether the tools chosen were the right ones for that alert.

Ignore entirely: writing quality, report formatting, confidence language, and
whether the conclusion turned out to be correct. A well-written report that
queried the wrong systems is a FAIL. A terse report that queried exactly the
right systems is a PASS.
"""

USER_PROMPT = """\
Alert the agent received:
{{span_input}}

Tools the agent called, and what came back:
{{span_output}}

Did the agent call the tools an experienced SRE would have called for this alert?

PASS when:
- The systems queried match the alert's domain (a Cloud Run alert queries Cloud
  Run health, its logs, and its recent deploys; a SQL alert queries the database).
- Time windows passed to those tools overlap the incident window.
- No obviously decisive check was skipped.

FAIL when:
- A different service or system was investigated than the alert names.
- The obvious first check for this alert type was never made.
- Tools were called with windows that cannot contain the incident.

Examples.

1. Alert: Cloud Run 5xx spike on api-west. Tools: cloud_run health on
   api-west, gcp_logging errors scoped to the spike window, deploy correlation.
   -> PASS. Domain, target and window all line up.

2. Alert: SQL blocking chain on WebCore. Tools: cloud_run health on an unrelated
   service, generic log search with no time filter.
   -> FAIL. Wrong system, and the window is unconstrained.

3. Alert: pipeline failure in ADO. Tools: ado pipeline detail, deploy
   correlation, known issues. Sentry not queried.
   -> PASS. Sentry adds nothing for a build failure; the route is right.

Answer with the boolean and one sentence naming the specific tool that was
wrongly chosen or wrongly omitted.
"""


def build_judge() -> LLMJudge:
    return LLMJudge(
        name=EVAL_NAME,
        provider="vertexai",
        model="claude-sonnet-4-6",
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        structured_output=BooleanStructuredOutput(
            description="Whether the agent selected appropriate tools for this alert",
            reasoning=True,
            pass_when=True,
        ),
    )

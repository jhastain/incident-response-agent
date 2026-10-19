"""Hallucination judge — does the report assert anything the evidence does not?

Overlaps verification_agent's citation check deliberately: this judge is
model-independent and catches narrative fabrication that a citation COUNT
misses. On 2026-07-27 a report read "NO DATA RETRIEVED" for the service the
alert was about and then supplied a metric anyway — that is the failure mode
this exists to catch.

Configured in Datadog against the Bifrost connector. This file is the
reviewable source of truth for the prompt.
"""

from __future__ import annotations

from ddtrace.llmobs._evaluators.llm_judge import BooleanStructuredOutput, LLMJudge

EVAL_NAME = "ir_report_hallucination"

SYSTEM_PROMPT = """\
You check an incident report against the tool output it was built from, and
flag any claim the evidence does not support.

A claim is UNSUPPORTED when the tool output does not contain it — including
when the output shows an error, a permission denial, or an empty result and the
report states a finding anyway. Precision is not evidence: a specific-looking
number with no matching tool output is exactly the failure you are looking for.

Do not reward fluency. Do not penalise a report for saying it could not
determine something — that is honest, not a hallucination.
"""

USER_PROMPT = """\
Tool output available to the agent:
{{span_input}}

The report it produced:
{{span_output}}

Does every factual claim in the report trace to that tool output?

Answer TRUE (no hallucination) when every number, timestamp, service name and
causal claim appears in the tool output, and gaps are stated as gaps.

Answer FALSE (hallucination present) when any of these hold:
- A metric, count or timestamp appears that is in no tool result.
- A tool returned an error or empty result, and the report reports a finding
  from it anyway.
- A number is real but is attributed to a time window the query never used.
- A causal claim is asserted as established with nothing behind it.

Examples.

1. Output: gcp_logging returns 1,247 errors between 08:02-08:32Z. Report: "1,247
   errors between 08:02 and 08:32 UTC".
   -> TRUE. Figure and window both match.

2. Output: cloud_run returns PERMISSION_DENIED. Report: "CPU sat at 94% through
   the incident".
   -> FALSE. The tool returned nothing; the figure is invented.

3. Output: datadog returns 43,525 error spans, query passed no time filter.
   Report: "43,525 error spans between 07:07 and 08:24".
   -> FALSE. Real number, fabricated window.

4. Output: sentry returns an empty result set. Report: "No Sentry issues were
   found for this service in the window."
   -> TRUE. Accurately reports an absence.

Name the specific unsupported claim in your reasoning.
"""


def build_judge() -> LLMJudge:
    return LLMJudge(
        name=EVAL_NAME,
        provider="vertexai",
        model="claude-sonnet-4-6",
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        structured_output=BooleanStructuredOutput(
            description="TRUE when every report claim is supported by tool output",
            reasoning=True,
            pass_when=True,
        ),
    )

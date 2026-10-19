"""Provision repo-defined LLM judges as Datadog custom evaluators via the REST API.

    uv run python scripts/publish_evaluators.py --dry-run
    uv run python scripts/publish_evaluators.py
    uv run python scripts/publish_evaluators.py --disable

Both judges must run through the org's Bifrost AI-Gateway connector, not a
direct provider. ddtrace's LLMObs.publish_evaluator() cannot express that: its
publish path only maps to openai/anthropic/azure_openai/vertexai/bedrock
(ddtrace/llmobs/_evaluators/llm_judge.py:142). The Datadog REST API is not
limited that way — it accepts an "llmproxy" integration_provider pointing at
an AI Gateway connector account, which is exactly the Bifrost connector. This
script therefore builds the same payload shape by hand and PUTs it directly,
instead of going through the SDK. The two evaluator files under
app/evaluators/ remain the reviewable source of truth for the prompts; this
script only repackages them for the API.

Endpoints (site is us5, NOT app.datadoghq.com):
    GET/PUT/DELETE https://api.us5.datadoghq.com/api/unstable/llm-obs/config/evaluators/custom/{eval_name}

Auth: DD-API-KEY / DD-APPLICATION-KEY headers, read from DD_API_KEY and
DD_APP_KEY by default (override with --api-key-env/--app-key-env).

PUT is create-or-update, so re-running this script is safe and will not
create duplicates.

Evaluators are published ENABLED at 10% sampling by default. Spend is bounded
by that sampling percentage, not by leaving them switched off. Pass --disable
to publish them turned off.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

# Importing anything under `app` runs app/__init__.py, which builds every agent's model
# and so reads the Bifrost gateway settings — required with no default since ADO-200022.
# This script only talks to the Datadog API; it never routes an LLM or MCP call through
# Bifrost (BIFROST_GATEWAY_ACCOUNT_ID below is a Datadog *connector* id, not a URL). So
# it supplies an unroutable RFC 2606 .invalid host rather than a real environment's URL:
# if a gateway call is ever added here it fails loudly instead of quietly reaching
# whichever environment happened to be named. Do not copy these into a calling path.
os.environ.setdefault("BIFROST_MCP_URL", "https://bifrost.invalid/mcp")
os.environ.setdefault("BIFROST_LLM_URL", "https://bifrost.invalid/v1")

from app.app_utils.dd_observability import resolved_ml_app
from app.evaluators.report_hallucination import (
    build_judge as build_hallucination,
)
from app.evaluators.tool_selection import (
    build_judge as build_tool_selection,
)

JUDGES = (build_tool_selection, build_hallucination)

DD_SITE = "us5.datadoghq.com"
API_BASE = f"https://api.{DD_SITE}/api/unstable/llm-obs/config/evaluators/custom"

# Confirmed live 2026-08-03 via GET on the existing "hallucination" evaluator
# (platform-ai): this account id resolves to the Bifrost-Prod-gateway connector.
BIFROST_GATEWAY_ACCOUNT_ID = "a9efe3b6-7e14-43e8-a233-31c07b135294"

# Matches the model already running in production for the "hallucination"
# evaluator against platform-ai. Keep judges on the same cheap model unless
# there's a specific reason to diverge.
JUDGE_MODEL = "vertex/claude-haiku-4-5"

# The API rejects an empty inference_params (temperature is required). Neither
# judge sets model_params, so fall back to the same defaults the working
# "hallucination" evaluator uses.
DEFAULT_INFERENCE_PARAMS = {
    "temperature": 1,
    "max_tokens": 4096,
    "top_p": 1,
    "presence_penalty": 0,
    "frequency_penalty": 0,
}

# Default judge sampling. Note this is a SECOND, independent knob from the
# agent's own DD_LLMOBS_SAMPLE_RATE trace sampling — the two MULTIPLY. Trace
# sampling is 1.0, so this percentage is the effective judged-run rate.
# 10% matches the org's existing platform-ai evaluators. Override with --sampling.
DEFAULT_SAMPLING_PERCENTAGE = 10
# Judges ship on. The sampling percentage is what bounds the spend, so
# publishing them disabled only means nobody remembers to turn them back on.
DEFAULT_ENABLED = True
# Judges are published per ml_app, and ml_app now varies by environment (test
# ships ir-agent). Resolving through DD_LLMOBS_ML_APP keeps the judge
# pointed at the same bucket the spans land in; set that var to publish for a
# different environment.
APPLICATION_NAME = resolved_ml_app()


def _build_payload(judge: Any, *, enabled: bool, sampling: int) -> dict[str, Any]:
    """Translate an LLMJudge into the REST API's evaluator_config shape.

    Field layout matches a live GET of the "hallucination" evaluator exactly
    (llm_judge_config.{inference_params,prompt_template,output_schema,
    parsing_type,assessment_criteria}, llm_provider, target) — nothing here is
    invented.
    """
    structured_output = judge._structured_output
    schema = structured_output.to_json_schema()

    output_schema = {
        "name": structured_output.label,
        "schema": schema,
        "strict": True,
    }

    assessment_criteria = None
    if structured_output.pass_when is not None:
        assessment_criteria = {"pass_when": structured_output.pass_when}

    llm_judge_config: dict[str, Any] = {
        "inference_params": judge._model_params or DEFAULT_INFERENCE_PARAMS,
        "prompt_template": [
            {"role": "system", "content": judge._system_prompt or ""},
            {"role": "user", "content": judge._user_prompt},
        ],
        "output_schema": output_schema,
        "parsing_type": "structured_output",
    }
    if assessment_criteria is not None:
        llm_judge_config["assessment_criteria"] = assessment_criteria

    return {
        "data": {
            "type": "evaluator_config",
            "attributes": {
                "eval_name": judge.name,
                "llm_judge_config": llm_judge_config,
                "llm_provider": {
                    "integration_provider": "llmproxy",
                    "integration_account_id": BIFROST_GATEWAY_ACCOUNT_ID,
                    "model_name": JUDGE_MODEL,
                },
                "target": {
                    "application_name": APPLICATION_NAME,
                    "enabled": enabled,
                    "sampling_percentage": sampling,
                    "root_spans_only": False,
                    "eval_scope": "span",
                },
            },
        }
    }


def _put_evaluator(eval_name: str, payload: dict[str, Any], api_key: str, app_key: str) -> None:
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Content-Type": "application/json",
    }
    resp = httpx.put(f"{API_BASE}/{eval_name}", headers=headers, json=payload, timeout=30.0)
    if resp.status_code != 200:
        print(f"Failed to publish {eval_name}: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not call the API")
    parser.add_argument(
        "--disable",
        action="store_true",
        help="publish with enabled=false (default: enabled, sampled per --sampling)",
    )
    parser.add_argument(
        "--sampling",
        type=int,
        default=DEFAULT_SAMPLING_PERCENTAGE,
        metavar="PCT",
        help=(
            f"percent of ingested traces to judge (default: {DEFAULT_SAMPLING_PERCENTAGE}). "
            "Independent of the agent's DD_LLMOBS_SAMPLE_RATE — the two multiply."
        ),
    )
    parser.add_argument("--api-key-env", default="DD_API_KEY", help="env var holding the Datadog API key")
    parser.add_argument(
        "--app-key-env", default="DD_APP_KEY", help="env var holding the Datadog application key"
    )
    args = parser.parse_args()

    if not 1 <= args.sampling <= 100:
        print(f"--sampling must be between 1 and 100, got {args.sampling}", file=sys.stderr)
        return 1

    enabled = DEFAULT_ENABLED and not args.disable
    payloads = [
        _build_payload(build(), enabled=enabled, sampling=args.sampling) for build in JUDGES
    ]

    if args.dry_run:
        for payload in payloads:
            print(json.dumps(payload, indent=2))
        return 0

    api_key = os.environ.get(args.api_key_env)
    app_key = os.environ.get(args.app_key_env)
    if not api_key or not app_key:
        print(
            f"{args.api_key_env} and {args.app_key_env} are both required to publish",
            file=sys.stderr,
        )
        return 1

    for payload in payloads:
        eval_name = payload["data"]["attributes"]["eval_name"]
        _put_evaluator(eval_name, payload, api_key, app_key)
        state = "enabled" if enabled else "disabled"
        print(f"{eval_name}: published against {APPLICATION_NAME} ({state}, {args.sampling}% sampling)")

    if not enabled:
        print("\nAll evaluators published DISABLED. Drop --disable to turn them on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Judge the A/B pair for one alert and emit a JSON verdict per variant.

Scores root-cause accuracy against tests/eval/accuracy-rubric.md (FROZEN — the
bands and hard rules below are copied from it verbatim; do not soften them).

The judge is an INDEPENDENT model (Sonnet, via the Bifrost gateway) — not the
model that produced either report — and is blind to which variant it is reading:
each report is presented as "Report A"/"Report B" with no model or branch label.
It never sees the agent's own verification block (no anchoring).

Usage: ab_judge.py <alert_file> <local_json> <deployed_json> <out_json>
"""
import json
import os
import re
import sys

import httpx

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-6")
BIFROST_URL = os.environ.get("BIFROST_LLM_URL", "https://llm-gateway.internal.example/v1")

_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
if os.path.exists(_ENV_FILE):
    for _line in open(_ENV_FILE, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

PROMPT = """You are an independent judge scoring incident-response reports for ROOT-CAUSE ACCURACY.

You are scoring two reports (A and B) produced for the SAME alert by two different
configurations of the same agent. You do not know which is which. Score each on its
own merits. Do NOT assume the longer report is better, and do NOT assume the shorter
one is better — score only what the rubric measures.

## THE ALERT
{alert}

## REPORT A
{report_a}

## EVIDENCE GATHERED FOR REPORT A (raw findings/synthesis; may be empty)
{evidence_a}

## REPORT B
{report_b}

## EVIDENCE GATHERED FOR REPORT B (raw findings/synthesis; may be empty)
{evidence_b}

## SCORING SCALE (0-100), per report — FROZEN RUBRIC, apply exactly
- 95-100: Correct root cause, specific and mechanism-level, fully supported by cited
  evidence in the findings. A responding engineer could act on it directly.
- 80-94: Correct root cause but under-specified (right system, vague mechanism) OR one
  supporting citation is weak/unverifiable.
- 60-79: Partially correct: identifies the right symptom/area but not the actual cause,
  or names a plausible cause not backed by the gathered evidence.
- 40-59: Wrong or speculative root cause; evidence does not support the conclusion; or
  hedged with no committed cause.
- 0-39: No root cause, or a root cause contradicted by the evidence.

## HARD RULES
1. Evidence-grounded. A confident root cause the findings do not actually support scores
   40-59, not higher. Fabricated or invalid citations cap the score at 59.
2. Mechanism over category. "Application errors on the login endpoint" restates the
   alert; it is not a root cause. The cause must explain WHY.
3. No credit for breadth. Listing possibilities without committing is 40-59.
4. An honest "cause undetermined, here is the specific missing artifact" is NOT
   automatically band 40-59 — if the evidence genuinely does not identify a cause and
   the report says so precisely while correctly scoping what IS known, score it on how
   well it characterizes the true situation.

Also record, per report, whether hedging language matches the evidence strength
(over-claiming certainty on thin evidence is a defect; correct hedging is not).

Return ONLY a JSON object, no prose, no code fence:
{{
  "a": {{"root_cause_score": <int>, "identified_cause": "<str>", "evidence_supported": <bool>,
         "hedging_appropriate": <bool>, "why": "<2 sentences max>"}},
  "b": {{"root_cause_score": <int>, "identified_cause": "<str>", "evidence_supported": <bool>,
         "hedging_appropriate": <bool>, "why": "<2 sentences max>"}},
  "true_cause_assessment": "<what the evidence actually shows, 2 sentences max>",
  "which_more_accurate": "A" | "B" | "tie"
}}"""


def evidence_blob(d: dict, limit: int = 14000) -> str:
    parts = []
    if d.get("synthesis"):
        parts.append("SYNTHESIS:\n" + str(d["synthesis"]))
    for k, v in (d.get("findings") or {}).items():
        parts.append(f"{k}:\n{v}")
    if d.get("tier2_findings"):
        parts.append("TIER2:\n" + str(d["tier2_findings"]))
    return ("\n\n".join(parts) or "(no raw evidence captured for this variant)")[:limit]


def main():
    alert_file, local_json, deployed_json, out_json = sys.argv[1:5]
    alert = open(alert_file, encoding="utf-8").read().strip()
    local = json.load(open(local_json, encoding="utf-8"))
    deployed = json.load(open(deployed_json, encoding="utf-8"))

    # Blind the judge: A = local (new code), B = deployed (current prod).
    body = {
        "model": JUDGE_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": PROMPT.format(
            alert=alert[:6000],
            report_a=(local.get("report_text") or "(no report produced)")[:12000],
            evidence_a=evidence_blob(local),
            report_b=(deployed.get("report_text") or "(no report produced)")[:12000],
            evidence_b=evidence_blob(deployed),
        )}],
    }
    with httpx.Client(timeout=300.0) as c:
        r = c.post(
            f"{BIFROST_URL}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['BIFROST_API_TOKEN']}",
                     "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]

    m = re.search(r"\{.*\}", text, re.DOTALL)
    verdict = json.loads(m.group(0)) if m else {"parse_error": text[:1000]}
    verdict["alert_file"] = os.path.basename(alert_file)
    verdict["judge_model"] = JUDGE_MODEL
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(verdict, f, indent=2)
    a = (verdict.get("a") or {}).get("root_cause_score")
    b = (verdict.get("b") or {}).get("root_cause_score")
    print(f"JUDGED {verdict['alert_file']} local(A)={a} deployed(B)={b} "
          f"winner={verdict.get('which_more_accurate')}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Score local_run.py outputs with the independent Sonnet judge (frozen rubric).

Local-run files embed report_text + findings inline, so no Firestore lookup — just
judge each against its alert. Usage: judge_local.py <alerts_dir> <results_dir>
Writes <results_dir>/judge_scores.json and prints scores.
"""
import glob
import json
import os
import re
import sys

import litellm

JUDGE_MODEL = "vertex_ai/claude-sonnet-4-6"
JUDGE_REGION = "us-east5"
RUBRIC = open(os.path.join(os.path.dirname(__file__), "accuracy-rubric.md"), encoding="utf-8").read()

HEAD = """You are an INDEPENDENT incident-response quality judge. You did NOT run this
investigation. Score ONLY the ROOT CAUSE 0-100 using the frozen rubric below.

""" + RUBRIC + """

Return STRICT JSON only:
{"root_cause_score": <int 0-100>, "identified_cause": "<one line>",
 "evidence_supported": <true|false>, "why": "<2-3 sentences citing specifics>"}"""


def judge(alert_text, d):
    findings = d.get("findings") or {}
    evidence = "\n\n".join(f"### {k}\n{str(v)[:3200]}" for k, v in findings.items())
    prompt = f"""{HEAD}

## THE INCIDENT (ground truth alert)
{alert_text}

## THE AGENT'S FINAL REPORT
{d.get('report_text','')[:6000]}

## THE AGENT'S SYNTHESIS
{str(d.get('synthesis',''))[:3000]}

## RAW EVIDENCE GATHERED (tier1 findings)
{evidence[:16000]}

## TIER2 FINDINGS
{str(d.get('tier2_findings',''))[:2000]}

Score the ROOT CAUSE now. STRICT JSON only."""
    r = litellm.completion(model=JUDGE_MODEL, messages=[{"role": "user", "content": prompt}],
                           vertex_ai_location=JUDGE_REGION, max_tokens=800, temperature=0)
    txt = r.choices[0].message.content.strip()
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    return json.loads(m.group(0)) if m else {"root_cause_score": -1, "why": txt[:300]}


def main():
    alerts_dir, results_dir = sys.argv[1], sys.argv[2]
    results = {}
    for p in sorted(glob.glob(f"{results_dir}/a*.json")):
        if p.endswith("judge_scores.json"):
            continue
        d = json.load(open(p, encoding="utf-8"))
        n = os.path.basename(p).replace("a", "").replace(".json", "")
        af = f"{alerts_dir}/alert_{n}.txt"
        if not os.path.exists(af):
            continue
        alert = open(af, encoding="utf-8").read().strip()
        if not d.get("report_text"):
            results[n] = {"root_cause_score": 0, "why": "no report"}
            print(f"alert{n}: 0 (no report)"); continue
        v = judge(alert, d)
        v["wall_s"] = d.get("wall_seconds")
        results[n] = v
        print(f"alert{n}: {v['root_cause_score']:>3} | evid={v.get('evidence_supported')} "
              f"| {d.get('wall_seconds')}s | {str(v.get('identified_cause'))[:60]}")
    scores = [r["root_cause_score"] for r in results.values()]
    if scores:
        print(f"\nmean={round(sum(scores)/len(scores))} min={min(scores)} all>=80: {all(s>=80 for s in scores)}")
    json.dump(results, open(f"{results_dir}/judge_scores.json", "w", encoding="utf-8"), indent=2)


if __name__ == "__main__":
    main()

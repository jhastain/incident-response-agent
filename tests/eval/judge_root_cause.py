#!/usr/bin/env python3
"""Independent root-cause accuracy judge.

Scores each of the 5 live alerts' final IR-agent report against the FROZEN rubric
in tests/eval/accuracy-rubric.md, using an INDEPENDENT judge model (Claude Sonnet
on Vertex) -- NOT the Haiku model that ran the investigation.

Ground truth = the original alert text (the incident). The judge reads the agent's
report + its raw findings/synthesis/tier2 evidence and decides whether the stated
root cause is (a) correct for THIS incident and (b) actually supported by the
gathered evidence. Server-side: report + findings are pulled from Firestore, mapped
to each alert by report-text match (retries write extra docs, so timestamp order is
NOT reliable -- we match on content).

Usage: judge_root_cause.py <BATCH_START_EPOCH>
Writes tests/eval/results/speed/rel4/judge_scores.json and prints a table.
"""
import json
import os
import re
import sys

import litellm
from google.cloud import firestore
from judge_from_files import resolve_project

# Derived from this file's location, not hardcoded — see ADO-200017. The literal here
# was <workspace>/aiops-agent-framework/incident_response_agent, the stale worktree
# that STILL EXISTS on disk. Because it exists and holds a plausible older copy of the
# fixtures, this judge read stale ground truth, scored it, printed a complete table, and
# wrote judge_scores.json into the other tree. ALERTS_DIR and RESULTS both interpolate
# REPO, so both sides moved together and stayed self-consistent — invisible from the
# output alone. `pwd -W`'s python equivalent is unnecessary here; abspath is native.
REPO = os.environ.get(
    "REPO",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
).replace("\\", "/")
ALERTS_DIR = os.environ.get("ALERTS_DIR", f"{REPO}/tests/eval/alerts")
RESULTS = os.environ.get("RESULTS_DIR", f"{REPO}/tests/eval/results/speed/rel4")
JUDGE_MODEL = "vertex_ai/claude-sonnet-4-6"
JUDGE_REGION = "us-east5"

# Distinctive signature per alert -> lets us map a Firestore report doc back to the
# alert it investigated (content match, retry-safe). Keyed by alert set (dir name).
_SIG_SETS = {
    "alerts": {
        "alert01": ["admin/login", "unauthorizedaccess", "error rate"],
        "alert02": ["hangfire", "octopus", "manual intervention"],
        "alert03": ["appsqlexception", "getting datatable"],
        "alert04": ["deadlineexceeded", "grpc", "httpunhandled"],
        "alert05": ["searchexception", "elastic"],
    },
}
ALERT_SIGNATURES = _SIG_SETS.get(os.path.basename(ALERTS_DIR), _SIG_SETS["alerts"])

RUBRIC = """You are an INDEPENDENT incident-response quality judge. You did NOT run
this investigation. Score ONLY the ROOT CAUSE, on a 0-100 scale.

BANDS (be strict):
- 95-100: Correct root cause, specific and mechanism-level, fully supported by
  evidence present in the findings. An engineer could act on it directly.
- 80-94 : Correct root cause but under-specified (right system, vague mechanism)
  OR one supporting citation is weak/unverifiable.
- 60-79 : Partially correct: right symptom/area but not the actual cause, OR a
  plausible cause not backed by the gathered evidence.
- 40-59 : Wrong/speculative root cause; evidence does not support it; OR hedged
  with no committed cause; OR fabricated/invalid citations.
- 0-39  : No root cause, or one contradicted by the evidence.

HARD RULES:
1. Evidence-grounded: a confident cause the findings do not support is <=59.
2. Mechanism over category: restating the alert ("errors on the login endpoint")
   is NOT a root cause. The cause must explain WHY.
3. No credit for breadth: listing possibilities without committing = 40-59.
4. Judge THIS incident only. If the report diagnoses a DIFFERENT incident than the
   alert describes (wrong error type/service/RC), that is a wrong root cause (<=39).

Return STRICT JSON only:
{"root_cause_score": <int 0-100>, "identified_cause": "<one line>",
 "true_cause_assessment": "<what the real cause is per the alert+evidence>",
 "evidence_supported": <true|false>, "cross_alert_bleed": <true|false>,
 "why": "<2-3 sentences citing specifics>"}"""


def load_alerts():
    out = {}
    for i in range(1, 6):
        with open(f"{ALERTS_DIR}/alert_0{i}.txt", encoding="utf-8") as f:
            out[f"alert0{i}"] = f.read().strip()
    return out


def pull_docs(start_epoch):
    # Same defect as judge_from_files.py (L8) — this second dev pin was not in the audit
    # table but is the identical shape, so it is fixed the same way rather than left as
    # a half-fixed judge. Both databases hold real data, so a wrong project scores a
    # different investigation of a similar alert instead of erroring.
    project = resolve_project()
    print(f"judging against Firestore project: {project}", flush=True)
    db = firestore.Client(project=project)
    docs = []
    for d in db.collection("incidents").stream():
        x = d.to_dict()
        ts = x.get("created_at")
        if not ts or ts.timestamp() < start_epoch:
            continue
        docs.append(x)
    docs.sort(key=lambda x: x["created_at"])
    return docs


def match_doc(alert_id, report):
    """How many of the alert's signatures appear in this report (score its fit)."""
    r = report.lower()
    return sum(1 for sig in ALERT_SIGNATURES[alert_id] if sig in r)


def pick_docs(alerts, docs):
    """Assign each alert its best-matching (latest) doc by signature overlap."""
    chosen = {}
    for aid in alerts:
        best, best_score, best_ts = None, 0, None
        for x in docs:
            sc = match_doc(aid, x.get("report", ""))
            ts = x["created_at"]
            # prefer higher signature match; tie-break on latest (final attempt)
            if sc > best_score or (sc == best_score and sc > 0 and (best_ts is None or ts > best_ts)):
                best, best_score, best_ts = x, sc, ts
        chosen[aid] = (best, best_score)
    return chosen


def judge(alert_text, doc):
    findings = doc.get("findings") or {}
    evidence = "\n\n".join(f"### {k}\n{str(v)[:3500]}" for k, v in findings.items())
    prompt = f"""{RUBRIC}

## THE INCIDENT (ground truth alert)
{alert_text}

## THE AGENT'S FINAL REPORT
{doc.get('report', '')[:6000]}

## THE AGENT'S SYNTHESIS
{str(doc.get('synthesis', ''))[:3000]}

## RAW EVIDENCE THE AGENT GATHERED (tier1 findings)
{evidence[:18000]}

## TIER2 FINDINGS
{str(doc.get('tier2_findings', ''))[:2500]}

Score the ROOT CAUSE now. STRICT JSON only."""
    r = litellm.completion(
        model=JUDGE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        vertex_ai_location=JUDGE_REGION,
        max_tokens=800,
        temperature=0,
    )
    txt = r.choices[0].message.content.strip()
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    return json.loads(m.group(0)) if m else {"root_cause_score": -1, "why": txt[:300]}


def main():
    start = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    alerts = load_alerts()
    docs = pull_docs(start)
    chosen = pick_docs(alerts, docs)
    results = {}
    print(f"batch docs pulled: {len(docs)}")
    for aid in sorted(alerts):
        doc, fit = chosen[aid]
        if not doc or fit == 0:
            print(f"{aid}: NO MATCHING DOC (fit={fit}) -- agent may not have diagnosed this incident")
            results[aid] = {"root_cause_score": 0, "why": "no matching report doc", "match_fit": fit}
            continue
        v = judge(alerts[aid], doc)
        v["match_fit"] = fit
        v["duration_s"] = (doc.get("telemetry") or {}).get("duration_seconds")
        results[aid] = v
        print(f"{aid}: {v['root_cause_score']:>3} | bleed={v.get('cross_alert_bleed')} "
              f"evid={v.get('evidence_supported')} dur={v['duration_s']}s | {v.get('identified_cause','')[:70]}")
    scores = [r["root_cause_score"] for r in results.values()]
    passing = all(s >= 95 for s in scores)
    durs = [r.get("duration_s") or 0 for r in results.values()]
    print("\n--- ROOT-CAUSE ACCURACY (independent Sonnet judge) ---")
    print(f"scores: {scores} | min={min(scores)} mean={round(sum(scores)/len(scores))} "
          f"| every>=95: {passing} | mean_dur={round(sum(durs)/len(durs))}s")
    with open(f"{RESULTS}/judge_scores.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"written: {RESULTS}/judge_scores.json")


if __name__ == "__main__":
    main()

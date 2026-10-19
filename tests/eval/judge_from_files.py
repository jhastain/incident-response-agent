#!/usr/bin/env python3
"""Score root-cause accuracy from the harness result files (authoritative per-alert).

Avoids the Firestore doc-matching confound (retries write extra docs). Each
live-alertNN.json is the final attempt for that alert and carries report_text. We
pull the matching full findings from Firestore by report signature to give the judge
the raw evidence, but the alert->report mapping comes from the harness file, 1:1.

Usage: judge_from_files.py <ALERTS_DIR> <RESULTS_DIR> <BATCH_START_EPOCH>
"""
import glob
import json
import os
import re
import sys

import litellm
from google.cloud import firestore

JUDGE_MODEL = "vertex_ai/claude-sonnet-4-6"
JUDGE_REGION = "us-east5"

RUBRIC = open(os.path.join(os.path.dirname(__file__), "accuracy-rubric.md"), encoding="utf-8").read()

_METADATA = os.path.join(os.path.dirname(__file__), "..", "..", "deployment_metadata.json")


def resolve_project() -> str:
    """Firestore project to read findings from — derived, not pinned (ADO-200021).

    Was `firestore.Client(project="aiops-dev-project")`. The agent writes to whichever
    project it is deployed in (currently test), and BOTH databases hold real data, so
    matches succeeded and scores were emitted against a *different* investigation of a
    similar alert — or the doc was silently dropped as unmatched, where the residual
    count reads as ordinary noise. Nothing errored.

    Override with FIRESTORE_PROJECT when judging a run against another environment.
    """
    override = os.environ.get("FIRESTORE_PROJECT", "").strip()
    if override:
        return override
    import subprocess

    try:
        with open(_METADATA, encoding="utf-8") as f:
            number = json.load(f)["remote_agent_runtime_id"].split("/")[1]
    except Exception as e:
        raise RuntimeError(
            f"cannot resolve the Firestore project: {_METADATA} is unreadable "
            f"({type(e).__name__}: {e}). Set FIRESTORE_PROJECT explicitly — reading the "
            f"wrong environment's incidents produces plausible scores for the wrong run."
        ) from e
    # deployment_metadata records the project NUMBER; the Firestore client wants the id.
    out = subprocess.run(
        ["gcloud", "projects", "describe", number, "--format=value(projectId)"],
        capture_output=True, text=True, shell=os.name == "nt",
    ).stdout.strip()
    if not out:
        raise RuntimeError(
            f"cannot translate project number {number} to a project id "
            f"(gcloud projects describe returned nothing). Set FIRESTORE_PROJECT explicitly."
        )
    return out

# Ground-truth incident per alert file, for the 3-test set (documentation only; the
# judge reads the alert text directly, this is just a human reference of what each is).
# alert_01 = Octopus deploy hang (1000001-rc, Step 47/50)
# alert_02 = ADO Teardown-Env pipeline failure
# alert_03 = CoreSvc login 5xx (loginwithauth0)

JUDGE_PROMPT_HEAD = """You are an INDEPENDENT incident-response quality judge. You did
NOT run this investigation. Score ONLY the ROOT CAUSE on 0-100 using the rubric below.

RUBRIC (authoritative, frozen):
""" + RUBRIC + """

Return STRICT JSON only:
{"root_cause_score": <int 0-100>, "identified_cause": "<one line>",
 "true_cause_assessment": "<the real cause per alert+evidence>",
 "evidence_supported": <true|false>, "diagnosed_correct_incident": <true|false>,
 "why": "<2-3 sentences citing specifics>"}"""


def latest_findings(db, start_epoch, report_text):
    """Find the Firestore doc whose report best matches this alert's report_text,
    so the judge can see the raw findings the agent gathered."""
    key = report_text[:200].lower()
    best, best_overlap = None, 0
    for d in db.collection("incidents").stream():
        x = d.to_dict()
        ts = x.get("created_at")
        if not ts or ts.timestamp() < start_epoch:
            continue
        rep = x.get("report", "").lower()
        # crude overlap: shared 20-char shingles from the report head
        overlap = sum(1 for i in range(0, min(len(key), 180), 20) if key[i:i + 20] in rep)
        if overlap > best_overlap:
            best, best_overlap = x, overlap
    return best


def judge(alert_text, report_text, findings_doc):
    findings = (findings_doc or {}).get("findings") or {}
    evidence = "\n\n".join(f"### {k}\n{str(v)[:3200]}" for k, v in findings.items())
    synth = str((findings_doc or {}).get("synthesis", ""))[:3000]
    tier2 = str((findings_doc or {}).get("tier2_findings", ""))[:2000]
    prompt = f"""{JUDGE_PROMPT_HEAD}

## THE INCIDENT (ground truth alert)
{alert_text}

## THE AGENT'S FINAL REPORT
{report_text[:6000]}

## THE AGENT'S SYNTHESIS
{synth}

## RAW EVIDENCE GATHERED (tier1 findings)
{evidence[:16000]}

## TIER2 FINDINGS
{tier2}

Score the ROOT CAUSE now. STRICT JSON only."""
    r = litellm.completion(model=JUDGE_MODEL, messages=[{"role": "user", "content": prompt}],
                           vertex_ai_location=JUDGE_REGION, max_tokens=800, temperature=0)
    txt = r.choices[0].message.content.strip()
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    return json.loads(m.group(0)) if m else {"root_cause_score": -1, "why": txt[:300]}


def main():
    alerts_dir, results_dir, start = sys.argv[1], sys.argv[2], float(sys.argv[3])
    project = resolve_project()
    print(f"judging against Firestore project: {project}", flush=True)
    db = firestore.Client(project=project)
    results = {}
    for p in sorted(glob.glob(f"{results_dir}/live-alert*.json")):
        aid = os.path.basename(p).replace("live-", "").replace(".json", "")
        d = json.load(open(p, encoding="utf-8"))
        n = aid.replace("alert", "")
        alert_text = open(f"{alerts_dir}/alert_{n}.txt", encoding="utf-8").read().strip()
        rt = d.get("report_text", "")
        if not rt:
            results[aid] = {"root_cause_score": 0, "why": "no report_text"}
            print(f"{aid}: 0 (no report)"); continue
        fdoc = latest_findings(db, start, rt)
        v = judge(alert_text, rt, fdoc)
        v["stream_cut"] = d.get("stream_cut")
        v["wall_s"] = d.get("wall_seconds")
        results[aid] = v
        print(f"{aid}: {v['root_cause_score']:>3} | correct_incident={v.get('diagnosed_correct_incident')} "
              f"evid={v.get('evidence_supported')} cut={d.get('stream_cut')} | {str(v.get('identified_cause'))[:64]}")
    scores = [r["root_cause_score"] for r in results.values()]
    print("\n--- ROOT-CAUSE ACCURACY (independent Sonnet judge, file-based) ---")
    print(f"scores: {scores} | min={min(scores)} mean={round(sum(scores)/len(scores))} | every>=95: {all(s>=95 for s in scores)}")
    json.dump(results, open(f"{results_dir}/judge_scores.json", "w", encoding="utf-8"), indent=2)
    print(f"written: {results_dir}/judge_scores.json")


if __name__ == "__main__":
    main()

"""ADO-200015 AC#5 — recover verification scores that were persisted as null.

DRY-RUN ONLY. This script has no write path and cannot be flagged into one: it
prints what a backfill would do and exits. Writing to a shared production store
needs explicit human approval, so the write is deliberately left unimplemented
rather than hidden behind a `--yes`.

Background
----------
Before the `_as_verification_dict` fix, `_as_dict` read only the FIRST fenced
block of the verifier's output. The verifier narrates before it scores, quoting
tool results in their own fences, so the score was frequently missed and the
document was written with `verification: null` (or, when a bare quoted string
parsed out of the prose, with an unrelated alert string in that field).

Measured live 2026-08-11: 124 documents in `incidents`, 23 malformed
(20 null + 3 str). 12 of the 23 scored MEDIUM — the band
`find_similar_incidents` filters on, so the loss is concentrated in exactly the
records that feed historical context back into future investigations.

Replaying the fixed parser over all 142 Agent Engine sessions recovered 27 of
30 previously-lost scores with 0 regressions and 0 tier changes on the 97 that
already worked. The residual 3 contain no score at all (the generation was
truncated) and no parser can recover them — a separate defect in
`verification_agent`, out of scope here.

Recommendation
--------------
Backfill all 23, after the parser fix has merged and deployed.

  1. Sessions are the only remaining source. If Agent Engine ages them out the
     data is gone permanently, so this is time-boxed.
  2. The 3 `str` documents are actively harmful rather than merely absent: they
     hold an alert string where consumers expect a map, and
     `find_similar_incidents` filters on `verification.combined_tier`. Those 3
     should be corrected regardless of the decision on the other 20.
  3. GCS must be updated in the same pass. All 20 null documents are equally
     null in `aiops-test-storage-incident-summaries-us`; a Firestore-only
     backfill would leave the two stores disagreeing.

Safety rules any eventual writer must honour:
  - Match each document to its session by nearest `updateTime`, assert the delta
    is under 5s, and cross-check the recovered `scored_at` against the document
    timestamp before writing.
  - Set `combined_tier = automated_tier`, exactly as persistence.py does.
  - Stamp `verification.backfilled_at` so backfilled scores stay distinguishable
    from live ones.
  - Never touch a document whose `verification` is already a populated dict.

Usage:
    uv run python scripts/backfill_verification_scores.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Reuse the shipped parser rather than a copy — a backfill that parses
# differently from production would write values production would never produce.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Importing anything under `app` runs app/__init__.py, which builds every agent's model
# and therefore reads the Bifrost gateway settings. Those are required with no default
# since ADO-200022. This script is read-only and never calls an LLM or an MCP tool, so it
# supplies a deliberately unroutable host rather than a real environment's URL: .invalid
# is reserved by RFC 2606 and cannot resolve, so if this ever DID reach the gateway path
# it fails immediately instead of quietly talking to whichever environment was named.
# Do not copy these values into anything that actually makes a call.
os.environ.setdefault("BIFROST_MCP_URL", "https://bifrost.invalid/mcp")
os.environ.setdefault("BIFROST_LLM_URL", "https://bifrost.invalid/v1")

from app.sub_agents.persistence import _as_verification_dict

MAX_CLOCK_DELTA_SECONDS = 5

_METADATA = os.path.join(os.path.dirname(__file__), "..", "deployment_metadata.json")


def _deployed_target() -> tuple[str, str]:
    """(project_number, engine_id) from the last deploy recorded in this checkout.

    Caveat worth knowing, documented by integrations/pagerduty/deploy.ps1: this file is
    rewritten wholesale per deploy with one flat `remote_agent_runtime_id` and no
    environment key, so whichever environment deployed most recently owns it. It is
    correct when the checkout's last deploy matches the environment you mean to inspect
    — which is why main() prints the resolved target before doing anything.
    """
    try:
        with open(_METADATA, encoding="utf-8") as f:
            parts = json.load(f)["remote_agent_runtime_id"].split("/")
        return parts[1], parts[-1]
    except Exception as e:
        raise SystemExit(
            f"cannot read {_METADATA} ({type(e).__name__}: {e}). Pass --project-number "
            f"and --engine explicitly; they are not defaulted because a stale engine id "
            f"turns 'no sessions matched' into a false 'the data aged out' conclusion."
        ) from e


def _project_id(project_number: str) -> str:
    """Translate a project number to its id (Firestore wants the id, not the number)."""
    import subprocess

    out = subprocess.run(
        ["gcloud", "projects", "describe", project_number, "--format=value(projectId)"],
        capture_output=True, text=True, shell=os.name == "nt",
    ).stdout.strip()
    if not out:
        raise SystemExit(
            f"cannot translate project number {project_number} to a project id. "
            f"Pass --project explicitly."
        )
    return out


def _load_sessions(engine: str, project_number: str, location: str) -> list[dict]:
    """List Agent Engine sessions and their state (read-only)."""
    import google.auth
    import google.auth.transport.requests as tr
    import requests

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(tr.Request())
    headers = {"Authorization": f"Bearer {creds.token}"}
    root = (
        f"https://{location}-aiplatform.googleapis.com/v1beta1/projects/{project_number}"
        f"/locations/{location}/reasoningEngines/{engine}/sessions"
    )

    sessions, token = [], None
    while True:
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        page = requests.get(root, headers=headers, params=params, timeout=60).json()
        for item in page.get("sessions", []):
            sessions.append({
                "id": item["name"].rsplit("/", 1)[-1],
                "update_time": item.get("updateTime"),
            })
        token = page.get("nextPageToken")
        if not token:
            break

    for session in sessions:
        detail = requests.get(f"{root}/{session['id']}", headers=headers, timeout=60).json()
        session["verification_result"] = (detail.get("sessionState") or {}).get("verification_result")
    return sessions


def _load_malformed_incidents(project: str) -> list[dict]:
    """Documents whose `verification` is not a populated dict (read-only)."""
    from google.cloud import firestore

    db = firestore.Client(project=project)
    rows = []
    for doc in db.collection("incidents").stream():
        data = doc.to_dict() or {}
        verification = data.get("verification")
        if isinstance(verification, dict) and verification:
            continue
        rows.append({
            "incident_id": doc.id,
            "current": type(verification).__name__ if verification is not None else "null",
            "category": data.get("category"),
            "severity": data.get("severity"),
            "timestamp": data.get("timestamp"),
            "update_time": getattr(doc, "update_time", None),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # --project-number and --engine derive from deployment_metadata.json (ADO-200017
    # idiom) rather than being pinned. The pinned pair was correct when written, but the
    # engine id changes on every redeploy while the project does not, so the two drifted
    # independently. After a redeploy the new engine has few sessions, the ±5s
    # MAX_CLOCK_DELTA_SECONDS match fails for most documents, and the report shows a large
    # "Unmatched" count — which reads as "the data aged out, this is time-boxed, act now",
    # exactly the urgency this script's docstring primes for. The opposite direction is
    # worse: a document can match a DIFFERENT run's session within 5s and propose the
    # wrong score. Both are wrong answers, not errors.
    # --project derives from the same source so the three cannot disagree. Leaving it
    # pinned while the number derived would let a run read one project's Firestore and
    # another project's sessions, and the ±5s matcher would report that as "unmatched".
    default_number, default_engine = _deployed_target()
    parser.add_argument("--project", default=_project_id(default_number))
    parser.add_argument("--project-number", default=default_number)
    parser.add_argument("--engine", default=default_engine)
    parser.add_argument("--location", default="us-central1")
    args = parser.parse_args()

    print("DRY RUN — this script never writes.\n")
    print(f"Target: project={args.project} project_number={args.project_number} "
          f"engine={args.engine} location={args.location}\n")

    incidents = _load_malformed_incidents(args.project)
    print(f"Firestore {args.project}/incidents: {len(incidents)} documents "
          f"with a missing or malformed verification block.")

    sessions = [s for s in _load_sessions(args.engine, args.project_number, args.location)
                if isinstance(s.get("verification_result"), str)]
    print(f"Agent Engine {args.engine}: {len(sessions)} sessions carrying verification_result.\n")

    by_update = sorted(
        (s for s in sessions if s.get("update_time")),
        key=lambda s: s["update_time"],
    )

    recoverable, unrecoverable, unmatched = [], [], []
    for row in incidents:
        doc_time = row.get("update_time")
        if doc_time is None or not by_update:
            unmatched.append(row)
            continue
        doc_iso = doc_time.isoformat() if hasattr(doc_time, "isoformat") else str(doc_time)
        match = min(by_update, key=lambda s: abs(_epoch(s["update_time"]) - _epoch(doc_iso)))
        delta = abs(_epoch(match["update_time"]) - _epoch(doc_iso))
        if delta > MAX_CLOCK_DELTA_SECONDS:
            unmatched.append({**row, "nearest_delta_s": round(delta, 1)})
            continue
        score = _as_verification_dict(match["verification_result"])
        tier = score.get("automated_tier") or score.get("combined_tier")
        target = recoverable if tier else unrecoverable
        target.append({**row, "session_id": match["id"], "delta_s": round(delta, 1),
                       "recovered_tier": tier, "scored_at": score.get("scored_at")})

    print(f"{'incident_id':46s} {'now':5s} {'sev':5s} {'delta':>6s}  tier")
    for row in sorted(recoverable, key=lambda r: r["incident_id"]):
        print(f"  {row['incident_id']:44s} {row['current']:5s} {row['severity'] or '?':5s} "
              f"{row['delta_s']:6.1f}  {row['recovered_tier']}")

    tiers: dict[str, int] = {}
    for row in recoverable:
        tiers[row["recovered_tier"]] = tiers.get(row["recovered_tier"], 0) + 1

    print(f"\nRecoverable:   {len(recoverable)}  {tiers}")
    print(f"Unrecoverable: {len(unrecoverable)}  (session matched, but it holds no score — "
          f"truncated generation, a separate defect in verification_agent)")
    print(f"Unmatched:     {len(unmatched)}  (no session inside "
          f"{MAX_CLOCK_DELTA_SECONDS}s of the document)")
    print("\nNo writes were made. Backfilling a shared store requires explicit approval; "
          "see the module docstring for the rules a writer must follow.")
    print("\nProposed payload for the first recoverable document:")
    if recoverable:
        print(json.dumps(recoverable[0], indent=2, default=str))
    return 0


def _epoch(value: str) -> float:
    import datetime

    text = str(value).replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(text).timestamp()


if __name__ == "__main__":
    raise SystemExit(main())

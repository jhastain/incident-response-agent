#!/usr/bin/env python3
"""Run one alert against the DEPLOYED IR Agent (Vertex Agent Engine) and save the report.

Mirrors integrations/pagerduty/agent_client.py: async_create_session then
stream_query (SSE), taking the longest report_agent text as the report. Auth is
ADC (google.auth.default), so it works without a `gcloud auth login` user token.

Usage: deployed_run.py <alert_file> <out_json>
"""
import json
import os
import sys
import time

import google.auth
import httpx
from google.auth.transport.requests import Request

# Derived from deployment_metadata.json, not hardcoded — same idiom as
# run_agent_eval.sh / run_live_alerts.sh (ADO-200017), extended here to the Python
# harnesses that ADO-200017 missed.
#
# The literal this replaces was the DEV engine, while dd_campaign.sh (30 runs) and
# ab_worker.sh / ab_all.sh call this script WITHOUT setting IR_AGENT_ENGINE_URL. So the
# entire A/B and Datadog-evidence campaign measured dev while every filename, log line
# and report called it "deployed". ab_worker.sh compounded it: local_run.py (which
# defaulted to the PROD project and prod Bifrost) against deployed_run.py (dev engine),
# labelled "local new-code vs deployed old-code" — three environments reported as a
# two-variant A/B.
_METADATA = os.path.join(os.path.dirname(__file__), "..", "..", "deployment_metadata.json")


def _resolve_engine() -> str:
    """IR_AGENT_ENGINE_URL, else the engine recorded by the last deploy from this checkout."""
    override = os.environ.get("IR_AGENT_ENGINE_URL", "").strip()
    if override:
        return override
    try:
        with open(_METADATA, encoding="utf-8") as f:
            runtime_id = json.load(f)["remote_agent_runtime_id"]
    except Exception as e:
        raise RuntimeError(
            f"cannot resolve the target engine: {_METADATA} is unreadable "
            f"({type(e).__name__}: {e}). Set IR_AGENT_ENGINE_URL explicitly. There is no "
            f"default — a wrong engine returns a complete, plausible report from the wrong build."
        ) from e
    return f"https://us-central1-aiplatform.googleapis.com/v1beta1/{runtime_id}"


ENGINE = _resolve_engine()
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
USER = "ab-eval@acme.example"
# The 600s streamQuery deadline is server-side; keep the client above it.
STREAM_TIMEOUT = float(os.environ.get("IR_AGENT_STREAM_TIMEOUT", "1200"))


def token() -> str:
    creds, _ = google.auth.default(scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def create_session(client: httpx.Client, tok: str) -> str:
    body = {"class_method": "async_create_session", "input": {"user_id": USER}}
    last = None
    for attempt in range(5):
        try:
            r = client.post(
                f"{ENGINE}:query",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json=body,
                timeout=60.0,
            )
            r.raise_for_status()
            sid = r.json().get("output", {}).get("id")
            if sid:
                return sid
            last = f"no session id: {r.text[:200]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        print(f"  session attempt {attempt+1} failed: {last}", flush=True)
    raise RuntimeError(f"session create failed: {last}")


def main():
    alert_file, out_json = sys.argv[1], sys.argv[2]
    alert = open(alert_file, encoding="utf-8").read().replace("\r", " ").strip()
    print(f"DEPLOYED target engine: {ENGINE}", flush=True)

    tok = token()
    report = verification = classification = ""
    tools = 0
    t0 = time.time()
    err = None
    with httpx.Client() as client:
        sid = create_session(client, tok)
        body = {
            "class_method": "stream_query",
            "input": {"user_id": USER, "session_id": sid, "message": alert},
        }
        try:
            with client.stream(
                "POST",
                f"{ENGINE}:streamQuery?alt=sse",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json=body,
                timeout=STREAM_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                for raw in resp.iter_lines():
                    line = raw.strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    author = event.get("author") or ""
                    for part in (event.get("content", {}) or {}).get("parts", []) or []:
                        if part.get("function_call"):
                            tools += 1
                        text = part.get("text")
                        if not text:
                            continue
                        if author == "report_agent" and len(text) > len(report):
                            report = text
                        elif author == "verification_agent" and len(text) > len(verification):
                            verification = text
                        elif author == "alert_classifier":
                            classification = text
        except Exception as e:
            err = f"{type(e).__name__}: {e}"

    res = {
        "variant": "deployed",
        # Persisted so a score is traceable to the engine that produced it. Both known
        # wrong-target incidents were invisible because no artifact recorded the target.
        "engine": ENGINE,
        "engine_id": ENGINE.rsplit("/", 1)[-1],
        "alert_file": os.path.basename(alert_file),
        "wall_seconds": round(time.time() - t0, 1),
        "tools": tools,
        "report_present": bool(report),
        "report_text": report,
        "report_chars": len(report),
        "verification_text": verification[:2000],
        "classification_text": classification[:1500],
        "error": err,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str)
    print(f"DEPLOYED {res['alert_file']} wall={res['wall_seconds']}s "
          f"report={res['report_present']} chars={res['report_chars']} err={err}", flush=True)


if __name__ == "__main__":
    main()

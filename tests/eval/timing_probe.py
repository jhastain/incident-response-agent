#!/usr/bin/env python
"""Per-agent timing probe: stream one scenario and record the wall-clock arrival
time of each event, grouped by author. Reveals which sequential phase consumes
the ~600s budget WITHOUT needing server-side telemetry or a redeploy.

Usage: timing_probe.py <scenario_id>
"""
import json
import os
import sys
import time
import urllib.request

# Derived from deployment_metadata.json — see ADO-200017. The literal here named engine
# 1000000000000000003, verified absent from all three projects, so this probe 404'd on its
# first call. Override with IR_AGENT_ENGINE_URL.
_METADATA = os.path.join(os.path.dirname(__file__), "..", "..", "deployment_metadata.json")
ENGINE = os.environ.get("IR_AGENT_ENGINE_URL", "").strip() or (
    "https://us-central1-aiplatform.googleapis.com/v1beta1/"
    + json.load(open(_METADATA, encoding="utf-8"))["remote_agent_runtime_id"]
)
print(f"timing_probe target engine: {ENGINE}", flush=True)
USER = "timing-probe@acme.example"

MSGS = {
    "sql-database-preferences-blocking": "SQL blocking chains in CoreSvc DB. AppSqlException events on JobCostingBudget endpoints, deadlocks in Hangfire jobs. sqlprod-c1, acme-production-100000. Started after enabling PC - Preferences Redesign feature flag for Builder 93767. 584+ AppSqlException events, query timeouts on cteChangeOrders (30s) and UpdSchedules (7.8s). MerchantPaymentLinks query storm with 17+ queries timing out. Escalated from Scrum Escalation Teams channel.",
    "cloud-infrastructure-startup-probe": "Octopus Deploy Step 47 stalled — Hangfire Cloud Run startup probe timeout in us-central1, acme-prodtest-100001. Container image imported but startup probe failed after 4m20s. 7/8 services healthy, recurringhighmem-us-c1 stuck. HealthCheckContainerError with zero container logs. Previous deploy to same service was healthy in 20.37s with identical config. Deployment entered Guided Failure mode after 60 polling attempts.",
    "application-errors-apigateway-mobile": "Bug 274816: Mobile clients unable to load schedule items after CalendarController ApiGateway migration. iOS shows all items for today only, Android shows 'failed to load schedule items'. Affected builders: 105759, 7509, 21024, 2546. acme-prodtest-100001. Issue surfaced in DC Certification Teams channel. DC build 1951205 also failing with Playwright timeouts. PR 102196 merged yesterday moving CalendarController from BaseController to ConvertedController.",
}

sid = sys.argv[1]
msg = MSGS[sid]
token = os.environ["GCLOUD_TOKEN"]

# create session
req = urllib.request.Request(
    ENGINE + ":query",
    data=json.dumps({"class_method": "async_create_session", "input": {"user_id": USER}}).encode(),
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
)
session_id = json.load(urllib.request.urlopen(req, timeout=30))["output"]["id"]

# stream, timestamping each event's arrival by author
url = ENGINE + ":streamQuery?alt=sse"
body = json.dumps({"class_method": "stream_query", "input": {"user_id": USER, "session_id": session_id, "message": msg}}).encode()
req = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})

t0 = time.time()
first_seen, last_seen, counts = {}, {}, {}
try:
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = round(time.time() - t0, 1)
            a = e.get("author") or "(none)"
            if a not in first_seen:
                first_seen[a] = t
            last_seen[a] = t
            counts[a] = counts.get(a, 0) + 1
except Exception as e:
    print("STREAM ERROR:", e)

total = round(time.time() - t0, 1)
print(f"\n=== {sid} — total {total}s ===")
print(f"{'author':26s} {'first_s':>8s} {'last_s':>8s} {'span_s':>8s} {'events':>7s}")
for a in sorted(first_seen, key=lambda k: first_seen[k]):
    span = round(last_seen[a] - first_seen[a], 1)
    print(f"{a:26s} {first_seen[a]:8.1f} {last_seen[a]:8.1f} {span:8.1f} {counts[a]:7d}")

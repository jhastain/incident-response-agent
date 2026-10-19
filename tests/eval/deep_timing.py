#!/usr/bin/env python
"""Deep per-call timing: stream one alert and record EVERY event with wall-clock
arrival time, author, and (for tool calls) the tool name. Then render a phase/
agent/call breakdown so we can see exactly where the ~530s goes.

Emits:
  1. Per-phase table (classifier / context / tier1 / synthesis / tier2 / report / verif / persist)
     with start, end, span, and % of total.
  2. Per-agent table within the parallel phases (first call, last event, span, #tool calls).
  3. Per-tool-call log (author, tool, arrival t, gap since previous) written to a JSON for the HTML.

Usage: GCLOUD_TOKEN=... deep_timing.py <alert_file> <out_json>
"""
import json
import os
import sys
import time
import urllib.request

# Derived from deployment_metadata.json — see ADO-200017. The literal here named engine
# 1000000000000000003, verified absent from all three projects, so this probe 404'd on its
# first call. The shell harnesses were fixed then; the two Python probes were missed.
# Override with IR_AGENT_ENGINE_URL.
_METADATA = os.path.join(os.path.dirname(__file__), "..", "..", "deployment_metadata.json")
ENGINE = os.environ.get("IR_AGENT_ENGINE_URL", "").strip() or (
    "https://us-central1-aiplatform.googleapis.com/v1beta1/"
    + json.load(open(_METADATA, encoding="utf-8"))["remote_agent_runtime_id"]
)
print(f"deep_timing target engine: {ENGINE}", flush=True)
USER = "deep-timing@acme.example"
TOKEN = os.environ["GCLOUD_TOKEN"]
alert_file = sys.argv[1]
out_json = sys.argv[2] if len(sys.argv) > 2 else "deep_timing.json"

msg = open(alert_file, encoding="utf-8").read().replace("\r", " ").replace("\n", " ").strip()

# session
req = urllib.request.Request(
    ENGINE + ":query",
    data=json.dumps({"class_method": "async_create_session", "input": {"user_id": USER}}).encode(),
    headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
)
sid = json.load(urllib.request.urlopen(req, timeout=30))["output"]["id"]

url = ENGINE + ":streamQuery?alt=sse"
body = json.dumps({"class_method": "stream_query", "input": {"user_id": USER, "session_id": sid, "message": msg}}).encode()
req = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})

t0 = time.time()
events = []  # (t, author, kind, detail)
try:
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data:"):
                s = s[5:].strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except json.JSONDecodeError:
                continue
            t = round(time.time() - t0, 1)
            a = e.get("author") or "(none)"
            parts = e.get("content", {}).get("parts", [])
            recorded = False
            for p in parts:
                fc = p.get("function_call")
                if fc:
                    events.append((t, a, "call", fc.get("name", "?")))
                    recorded = True
                fr = p.get("function_response")
                if fr:
                    events.append((t, a, "resp", (fr.get("name") or "?")))
                    recorded = True
                txt = p.get("text")
                if txt and txt.strip():
                    events.append((t, a, "text", f"{len(txt)}chars"))
                    recorded = True
            if not recorded:
                events.append((t, a, "event", ""))
except Exception as ex:
    events.append((round(time.time() - t0, 1), "(error)", "error", str(ex)[:100]))

total = round(time.time() - t0, 1)

# ---- Phase mapping ----
PHASE = {
    "alert_classifier": "1-classify",
    "infra_context": "2-infra_context",
    "historical_context": "3-tier1(parallel)",
    "cloud_run_health": "3-tier1(parallel)", "compute_engine_health": "3-tier1(parallel)",
    "gcp_logging": "3-tier1(parallel)", "gcp_metrics": "3-tier1(parallel)",
    "datadog_observability": "3-tier1(parallel)", "sentry_error": "3-tier1(parallel)",
    "deploy_correlation": "3-tier1(parallel)", "teams_context": "3-tier1(parallel)",
    "known_issues": "3-tier1(parallel)",
    "synthesis_agent": "4-synthesis",
    "tier2_dispatcher": "5-tier2", "sql_dba_agent": "5-tier2", "codebase_agent": "5-tier2",
    "cache_agent": "5-tier2", "pubsub_agent": "5-tier2", "auth_security_agent": "5-tier2",
    "report_agent": "6-report",
    "verification_agent": "7-verification",
    "persistence_agent": "8-persistence",
}

# per-agent aggregation
agents = {}
for t, a, kind, detail in events:
    d = agents.setdefault(a, {"first": t, "last": t, "calls": 0, "resps": 0, "texts": 0})
    d["first"] = min(d["first"], t)
    d["last"] = max(d["last"], t)
    if kind == "call": d["calls"] += 1
    elif kind == "resp": d["resps"] += 1
    elif kind == "text": d["texts"] += 1

# per-phase aggregation (by min-first / max-last of member agents)
phases = {}
for a, d in agents.items():
    ph = PHASE.get(a, f"?-{a}")
    p = phases.setdefault(ph, {"first": d["first"], "last": d["last"], "calls": 0, "agents": set()})
    p["first"] = min(p["first"], d["first"])
    p["last"] = max(p["last"], d["last"])
    p["calls"] += d["calls"]
    p["agents"].add(a)

out = {
    "total_seconds": total,
    "phases": [
        {"phase": ph, "start": p["first"], "end": p["last"], "span": round(p["last"] - p["first"], 1),
         "tool_calls": p["calls"], "agents": sorted(p["agents"])}
        for ph, p in sorted(phases.items())
    ],
    "agents": [
        {"agent": a, "phase": PHASE.get(a, "?"), "first": d["first"], "last": d["last"],
         "span": round(d["last"] - d["first"], 1), "tool_calls": d["calls"], "responses": d["resps"], "texts": d["texts"]}
        for a, d in sorted(agents.items(), key=lambda kv: kv[1]["first"])
    ],
    "calls": [{"t": t, "author": a, "kind": kind, "detail": detail} for (t, a, kind, detail) in events],
}
json.dump(out, open(out_json, "w", encoding="utf-8"), indent=2)

# ---- console tables ----
print(f"\n=== TOTAL {total}s ===")
print(f"{'PHASE':22s} {'start':>7s} {'end':>7s} {'span':>7s} {'%':>5s} {'calls':>6s}")
for p in out["phases"]:
    pct = round(100 * p["span"] / total, 1) if total else 0
    print(f"{p['phase']:22s} {p['start']:7.1f} {p['end']:7.1f} {p['span']:7.1f} {pct:5.1f} {p['tool_calls']:6d}")
print(f"\n{'AGENT':24s} {'phase':18s} {'first':>7s} {'last':>7s} {'span':>7s} {'calls':>6s}")
for a in out["agents"]:
    print(f"{a['agent']:24s} {a['phase']:18s} {a['first']:7.1f} {a['last']:7.1f} {a['span']:7.1f} {a['tool_calls']:6d}")
print(f"\nwrote {out_json} ({len(out['calls'])} events)")

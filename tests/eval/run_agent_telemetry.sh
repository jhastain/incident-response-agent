#!/usr/bin/env bash
# E2E instrumentation run: captures PER-AGENT tool-call / event / token telemetry
# from the deployed IR Agent for all 3 fixed scenarios (parallel).
#
# Writes ONLY compact JSON summaries — raw SSE streams are parsed then discarded.
# NEVER cat the stream-*.txt files; they overflow context.
#
# Output: tests/eval/results/telemetry-<sid>.json  (one per scenario)
#         tests/eval/results/telemetry-breakdown.json (combined per-agent table)
set -u

USER_ID="first.last@acme.example"
PY="/c/python313/python"
# Derived, not hardcoded — see ADO-200017.
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && { pwd -W 2>/dev/null || pwd; })}"
ENGINE_ID="${ENGINE_ID:-$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['remote_agent_runtime_id'])" "${REPO}/deployment_metadata.json")}"
ENGINE="https://us-central1-aiplatform.googleapis.com/v1beta1/${ENGINE_ID}"
RESULTS="${REPO}/tests/eval/results"
TMP="${REPO}/tests/eval/.telemetry-tmp"
mkdir -p "$TMP" "$RESULTS"

TOKEN="$(gcloud auth print-access-token)"

run_scenario() {
  local sid="$1"; local msg="$2"
  local stream="${TMP}/stream-${sid}.txt"
  local res="${RESULTS}/telemetry-${sid}.json"

  local sess_json session_id
  sess_json=$(curl -s -X POST "${ENGINE}:query" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -d "{\"class_method\": \"async_create_session\", \"input\": {\"user_id\": \"${USER_ID}\"}}")
  session_id=$(printf '%s' "$sess_json" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d.get('output',{}).get('id') or d.get('session_id') or '')" 2>/dev/null)
  if [ -z "$session_id" ]; then
    printf '{"scenario_id":"%s","error":"session creation failed"}\n' "$sid" > "$res"; return
  fi

  local start_t end_t wall
  start_t=$(date +%s)
  "$PY" - "$ENGINE" "$TOKEN" "$USER_ID" "$session_id" "$msg" "$stream" <<'PYEOF'
import sys, json, urllib.request
engine, token, user_id, session_id, msg, stream_path = sys.argv[1:7]
url = engine + ":streamQuery?alt=sse"
body = json.dumps({"class_method":"stream_query","input":{"user_id":user_id,"session_id":session_id,"message":msg}}).encode()
req = urllib.request.Request(url, data=body, headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
try:
    with urllib.request.urlopen(req, timeout=900) as r, open(stream_path,"wb") as f:
        for chunk in r:
            f.write(chunk)
except Exception as e:
    with open(stream_path,"ab") as f:
        f.write(("\n{\"__client_error__\":\""+str(e).replace('"',"'")+"\"}\n").encode())
PYEOF
  end_t=$(date +%s); wall=$((end_t - start_t))

  "$PY" - "$sid" "$stream" "$wall" "$res" <<'PYEOF'
import sys, json
from collections import defaultdict
sid, stream_path, wall, res_path = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
events = []
with open(stream_path, 'r', encoding='utf-8', errors='replace') as f:
    for line in f:
        line = line.strip()
        if line.startswith('data:'): line = line[5:].strip()
        if not line: continue
        try: events.append(json.loads(line))
        except json.JSONDecodeError: continue

# per-author aggregation
tools = defaultdict(int)      # function_call parts
resp  = defaultdict(int)      # function_response parts (tool results returned)
evc   = defaultdict(int)      # event count
toks  = defaultdict(int)      # max total_token_count seen on that author's events
tool_names = defaultdict(lambda: defaultdict(int))  # author -> {tool_name: count}
for e in events:
    a = e.get('author') or '(none)'
    evc[a] += 1
    um = e.get('usage_metadata', {})
    if um.get('total_token_count'):
        toks[a] = max(toks[a], um['total_token_count'])
    for part in e.get('content', {}).get('parts', []):
        fc = part.get('function_call')
        if fc:
            tools[a] += 1
            tool_names[a][fc.get('name','?')] += 1
        if part.get('function_response'):
            resp[a] += 1

stream_cut = any('UNAVAILABLE' in json.dumps(e) or '__client_error__' in e for e in events)
overall_tokens = max([0] + [um for um in toks.values()])

authors = sorted(set(list(evc.keys())), key=lambda k: (-tools[k], k))
per_agent = []
for a in authors:
    per_agent.append({
        "author": a,
        "tool_calls": tools[a],
        "tool_responses": resp[a],
        "events": evc[a],
        "max_tokens": toks[a],
        "tools": dict(sorted(tool_names[a].items(), key=lambda x:-x[1])),
    })

result = {
    "scenario_id": sid,
    "wall_clock_seconds": wall,
    "stream_cut": stream_cut,
    "total_events": len(events),
    "total_tool_calls": sum(tools.values()),
    "total_tokens": overall_tokens,
    "per_agent": per_agent,
}
with open(res_path,'w',encoding='utf-8') as f:
    json.dump(result, f, indent=2)
print("DONE "+sid+" tools="+str(sum(tools.values()))+" wall="+str(wall)+" cut="+str(stream_cut))
PYEOF
  rm -f "$stream"
}

MSG1='Octopus Deploy Step 47 stalled — Hangfire Cloud Run startup probe timeout in us-central1, acme-prodtest-100001. Container image imported but startup probe failed after 4m20s. 7/8 services healthy, recurringhighmem-us-c1 stuck. HealthCheckContainerError with zero container logs. Previous deploy to same service was healthy in 20.37s with identical config. Deployment entered Guided Failure mode after 60 polling attempts.'
MSG2='SQL blocking chains in CoreSvc DB. AppSqlException events on JobCostingBudget endpoints, deadlocks in Hangfire jobs. sqlprod-c1, acme-production-100000. Started after enabling PC - Preferences Redesign feature flag for Builder 93767. 584+ AppSqlException events, query timeouts on cteChangeOrders (30s) and UpdSchedules (7.8s). MerchantPaymentLinks query storm with 17+ queries timing out. Escalated from Scrum Escalation Teams channel.'
MSG3="Bug 274816: Mobile clients unable to load schedule items after CalendarController ApiGateway migration. iOS shows all items for today only, Android shows 'failed to load schedule items'. Affected builders: 105759, 7509, 21024, 2546. acme-prodtest-100001. Issue surfaced in DC Certification Teams channel. DC build 1951205 also failing with Playwright timeouts. PR 102196 merged yesterday moving CalendarController from BaseController to ConvertedController."

run_scenario "cloud-infrastructure-startup-probe" "$MSG1" & P1=$!
run_scenario "sql-database-preferences-blocking" "$MSG2" & P2=$!
run_scenario "application-errors-apigateway-mobile" "$MSG3" & P3=$!
wait $P1 $P2 $P3

# combined breakdown
"$PY" - "$RESULTS" <<'PYEOF'
import sys, json, os
results = sys.argv[1]
combined = {"generated_from":"run_agent_telemetry.sh","scenarios":{}}
agg = {}
for sid in ["cloud-infrastructure-startup-probe","sql-database-preferences-blocking","application-errors-apigateway-mobile"]:
    p = os.path.join(results, f"telemetry-{sid}.json")
    if not os.path.exists(p): continue
    d = json.load(open(p, encoding='utf-8'))
    combined["scenarios"][sid] = {"wall":d.get("wall_clock_seconds"),"cut":d.get("stream_cut"),
                                  "total_tool_calls":d.get("total_tool_calls"),"total_tokens":d.get("total_tokens")}
    for a in d.get("per_agent", []):
        row = agg.setdefault(a["author"], {"author":a["author"],"s1":0,"s2":0,"s3":0,"total":0})
        key = {"cloud-infrastructure-startup-probe":"s1","sql-database-preferences-blocking":"s2","application-errors-apigateway-mobile":"s3"}[sid]
        row[key] = a["tool_calls"]; row["total"] += a["tool_calls"]
combined["per_agent_tool_calls"] = sorted(agg.values(), key=lambda r:-r["total"])
json.dump(combined, open(os.path.join(results,"telemetry-breakdown.json"),"w",encoding='utf-8'), indent=2)
print("BREAKDOWN written")
for r in combined["per_agent_tool_calls"]:
    print(f"  {r['author']:<26} s1={r['s1']:>3} s2={r['s2']:>3} s3={r['s3']:>3} total={r['total']:>3}")
PYEOF

rmdir "$TMP" 2>/dev/null || true
echo "ALL SCENARIOS COMPLETE"

#!/usr/bin/env bash
# Efficiency-loop eval capture: runs all 3 fixed scenarios against the deployed
# IR Agent (parallel) and writes ONE compact eval JSON per scenario containing
# BOTH performance telemetry AND quality signals (report text, classification,
# verification presence) so a judge can score PASS/FAIL.
#
# Writes ONLY compact JSON — raw SSE streams parsed then deleted. NEVER cat streams.
# Output: <RESULTS>/eval-<sid>.json  and  <RESULTS>/eval-summary.json
#
# Usage: run_agent_eval.sh <RESULTS_DIR>
set -u

USER_ID="first.last@acme.example"
PY="/c/python313/python"
# Derived, not hardcoded — see ADO-200017. The old literals named a deleted engine
# (1000000000000000003) and a stale worktree that still exists on disk, so runs
# silently used the wrong tree instead of failing.
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && { pwd -W 2>/dev/null || pwd; })}"
ENGINE_ID="${ENGINE_ID:-$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['remote_agent_runtime_id'])" "${REPO}/deployment_metadata.json")}"
ENGINE="https://us-central1-aiplatform.googleapis.com/v1beta1/${ENGINE_ID}"
RESULTS="${1:-${REPO}/tests/eval/results}"
TMP="${RESULTS}/.eval-tmp"
mkdir -p "$TMP" "$RESULTS"

TOKEN="$(gcloud auth print-access-token)"

run_scenario_once() {
  local sid="$1"; local msg="$2"
  local stream="${TMP}/stream-${sid}.txt"
  local res="${RESULTS}/eval-${sid}.json"

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
import sys, json, re
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

tools = defaultdict(int); resp = defaultdict(int); evc = defaultdict(int); toks = defaultdict(int)
tool_names = defaultdict(lambda: defaultdict(int))
report_text = ""; classification_text = ""; verification_text = ""
for e in events:
    a = e.get('author') or '(none)'
    evc[a] += 1
    um = e.get('usage_metadata', {})
    if um.get('total_token_count'): toks[a] = max(toks[a], um['total_token_count'])
    for part in e.get('content', {}).get('parts', []):
        fc = part.get('function_call')
        if fc:
            tools[a] += 1; tool_names[a][fc.get('name','?')] += 1
        if part.get('function_response'): resp[a] += 1
        t = part.get('text')
        if t:
            if a == 'report_agent' and len(t) > len(report_text): report_text = t
            if a == 'alert_classifier': classification_text = t
            if a == 'verification_agent' and len(t) > len(verification_text): verification_text = t

stream_cut = any('UNAVAILABLE' in json.dumps(e) or '__client_error__' in e for e in events)
overall_tokens = max([0]+list(toks.values()))

# quality: 4 report sections. ADO-200018 cut the report to TL;DR, Incident Summary,
# What Happened, and Key Findings (STRONG tier only). Root Cause, Timeline and
# Recommended Actions were removed from the template, so scoring them made the gate
# permanently unsatisfiable — the exact failure mode ADO-200013 fixed once already.
# has_mechanism replaces has_root_cause: the causal claim did not go away, it moved
# into the first Key Findings bullet as "Mechanism: ...".
# ADO-200020 restored a trailing **Links:** section and deliberately did NOT add a
# sixth has_links key. That section is correctly ABSENT when a run retrieves no
# verifiable URL, so scoring it would mark an honest report incomplete and trigger
# the retry loop below — the same unsatisfiable-gate failure ADO-200013 and
# ADO-200018 each had to undo. Links are asserted in tests/unit/test_report_format.py
# (TestLinksSection), where the empty case can be expressed as valid.
rt = report_text.lower()
sections = {
  "has_tldr": "**tl;dr:**" in rt,
  "has_severity_header": bool(re.search(r'sev|🟢|🟠|🔴', report_text)),
  "has_what_happened": "what happened" in rt,
  "has_key_findings": "key findings" in rt,
  "has_mechanism": "mechanism:" in rt,
}
sections_score = sum(1 for v in sections.values() if v)

# telemetry, measured at the source rather than read back out of the report text
telemetry_captured = bool(toks) and overall_tokens > 0 and sum(tools.values()) > 0

# parse classification json if possible
classif = {}
try: classif = json.loads(classification_text)
except Exception: pass

authors = sorted(evc.keys(), key=lambda k:(-tools[k],k))
per_agent = [{"author":a,"tool_calls":tools[a],"tool_responses":resp[a],"events":evc[a],
             "max_tokens":toks[a],"tools":dict(sorted(tool_names[a].items(),key=lambda x:-x[1]))} for a in authors]

result = {
  "scenario_id": sid,
  "wall_clock_seconds": wall,
  "stream_cut": stream_cut,
  "total_events": len(events),
  "total_tool_calls": sum(tools.values()),
  "total_tokens": overall_tokens,
  # PASS signals
  "report_present": bool(report_text),
  "report_sections": sections,
  "report_sections_score": sections_score,
  "telemetry_captured": telemetry_captured,
  "verification_present": bool(verification_text),
  "classification": {
     "category": classif.get("category"),
     "severity": classif.get("severity"),
     "service": classif.get("service_name"),
     "environment": classif.get("environment"),
  },
  # for judge (truncated to keep files small but scorable)
  "report_text": report_text[:9000],
  "verification_text": verification_text[:2500],
  "per_agent": per_agent,
}
with open(res_path,'w',encoding='utf-8') as f: json.dump(result, f, indent=2)
print(f"DONE {sid} tools={sum(tools.values())} tok={overall_tokens} wall={wall} cut={stream_cut} report={bool(report_text)} sec={sections_score}/5 tele={telemetry_captured} verif={bool(verification_text)}")
PYEOF
  rm -f "$stream"
}

# Retry wrapper: an investigation is COMPLETE only when it produced a full report
# (5/5 section checks — see the four-section report, ADO-200018), telemetry captured
# on the event stream, a verification score,
# and did not have its stream cut. The two
# residual failure modes are transient — a deadline overrun when tool-count and
# backend latency stack unfavorably, and a platform instance-recycle mid-stream —
# so a fresh-session retry converts them to success. Each attempt uses a NEW
# session (run_scenario_once creates one) because a cut session may be poisoned.
# RETRIES = max attempts (default 3). Set RETRIES=1 to measure single-attempt rate.
is_complete() {
  local res="$1"
  "$PY" - "$res" <<'PYEOF'
import sys, json, os
p = sys.argv[1]
if not os.path.exists(p):
    sys.exit(1)
try:
    d = json.load(open(p, encoding='utf-8'))
except Exception:
    sys.exit(1)
ok = (d.get('report_present') and d.get('report_sections_score') == 5
      and d.get('telemetry_captured')
      and d.get('verification_present') and not d.get('stream_cut'))
sys.exit(0 if ok else 1)
PYEOF
}

run_scenario() {
  local sid="$1"; local msg="$2"
  local res="${RESULTS}/eval-${sid}.json"
  local max="${RETRIES:-3}"
  local attempt=1
  while [ "$attempt" -le "$max" ]; do
    [ "$attempt" -gt 1 ] && echo "RETRY ${sid} attempt ${attempt}/${max} (previous incomplete)"
    run_scenario_once "$sid" "$msg"
    if is_complete "$res"; then
      # stamp attempts-used onto the result for campaign accounting
      "$PY" - "$res" "$attempt" <<'PYEOF'
import sys, json
p, a = sys.argv[1], int(sys.argv[2])
d = json.load(open(p, encoding='utf-8'))
d['attempts_used'] = a
json.dump(d, open(p, 'w', encoding='utf-8'), indent=2)
PYEOF
      [ "$attempt" -gt 1 ] && echo "RECOVERED ${sid} on attempt ${attempt}"
      return 0
    fi
    attempt=$((attempt + 1))
  done
  # exhausted retries — annotate the last (incomplete) result
  "$PY" - "$res" "$max" <<'PYEOF'
import sys, json, os
p, m = sys.argv[1], int(sys.argv[2])
if os.path.exists(p):
    try:
        d = json.load(open(p, encoding='utf-8'))
    except Exception:
        d = {}
    d['attempts_used'] = m
    d['retries_exhausted'] = True
    json.dump(d, open(p, 'w', encoding='utf-8'), indent=2)
PYEOF
  echo "EXHAUSTED ${sid} after ${max} attempts (still incomplete)"
  return 1
}

MSG1='Octopus Deploy Step 47 stalled — Hangfire Cloud Run startup probe timeout in us-central1, acme-prodtest-100001. Container image imported but startup probe failed after 4m20s. 7/8 services healthy, recurringhighmem-us-c1 stuck. HealthCheckContainerError with zero container logs. Previous deploy to same service was healthy in 20.37s with identical config. Deployment entered Guided Failure mode after 60 polling attempts.'
MSG2='SQL blocking chains in CoreSvc DB. AppSqlException events on JobCostingBudget endpoints, deadlocks in Hangfire jobs. sqlprod-c1, acme-production-100000. Started after enabling PC - Preferences Redesign feature flag for Builder 93767. 584+ AppSqlException events, query timeouts on cteChangeOrders (30s) and UpdSchedules (7.8s). MerchantPaymentLinks query storm with 17+ queries timing out. Escalated from Scrum Escalation Teams channel.'
MSG3="Bug 274816: Mobile clients unable to load schedule items after CalendarController ApiGateway migration. iOS shows all items for today only, Android shows 'failed to load schedule items'. Affected builders: 105759, 7509, 21024, 2546. acme-prodtest-100001. Issue surfaced in DC Certification Teams channel. DC build 1951205 also failing with Playwright timeouts. PR 102196 merged yesterday moving CalendarController from BaseController to ConvertedController."

# SCENARIO=<id> runs only that one scenario (single-run testing). Valid ids:
# cloud-infrastructure-startup-probe | sql-database-preferences-blocking |
# application-errors-apigateway-mobile. Default (unset) runs all three.
# SEQUENTIAL=1 runs one scenario at a time (matches production /investigate,
# which handles a single incident per invocation). Default is parallel.
if [ -n "${SCENARIO:-}" ]; then
  case "$SCENARIO" in
    cloud-infrastructure-startup-probe) run_scenario "$SCENARIO" "$MSG1" ;;
    sql-database-preferences-blocking)   run_scenario "$SCENARIO" "$MSG2" ;;
    application-errors-apigateway-mobile)     run_scenario "$SCENARIO" "$MSG3" ;;
    *) echo "Unknown SCENARIO: $SCENARIO" >&2; exit 2 ;;
  esac
elif [ "${SEQUENTIAL:-0}" = "1" ]; then
  run_scenario "cloud-infrastructure-startup-probe" "$MSG1"
  run_scenario "sql-database-preferences-blocking" "$MSG2"
  run_scenario "application-errors-apigateway-mobile" "$MSG3"
else
  run_scenario "cloud-infrastructure-startup-probe" "$MSG1" & P1=$!
  run_scenario "sql-database-preferences-blocking" "$MSG2" & P2=$!
  run_scenario "application-errors-apigateway-mobile" "$MSG3" & P3=$!
  wait $P1 $P2 $P3
fi

"$PY" - "$RESULTS" <<'PYEOF'
import sys, json, os
results = sys.argv[1]
summ = {"scenarios":{}}
allpass = True
for sid in ["cloud-infrastructure-startup-probe","sql-database-preferences-blocking","application-errors-apigateway-mobile"]:
    p = os.path.join(results, f"eval-{sid}.json")
    if not os.path.exists(p): allpass=False; summ["scenarios"][sid]={"error":"missing"}; continue
    d = json.load(open(p,encoding='utf-8'))
    if "error" in d: allpass=False; summ["scenarios"][sid]=d; continue
    summ["scenarios"][sid] = {
      "tool_calls":d["total_tool_calls"],"tokens":d["total_tokens"],"wall":d["wall_clock_seconds"],
      "cut":d["stream_cut"],"report":d["report_present"],"sections":d["report_sections_score"],
      "tele":d.get("telemetry_captured"),
      "verif":d["verification_present"],"classification":d["classification"],
    }
summ["total_tokens"] = sum(s.get("tokens",0) for s in summ["scenarios"].values() if isinstance(s,dict))
summ["total_tool_calls"] = sum(s.get("tool_calls",0) for s in summ["scenarios"].values() if isinstance(s,dict))
json.dump(summ, open(os.path.join(results,"eval-summary.json"),"w",encoding='utf-8'), indent=2)
print("EVAL-SUMMARY", json.dumps(summ["scenarios"], indent=0)[:1500])
PYEOF
rmdir "$TMP" 2>/dev/null || true
echo "ALL SCENARIOS COMPLETE"

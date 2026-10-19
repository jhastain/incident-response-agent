#!/usr/bin/env bash
# Speed-loop harness: run the 5 live Platform-Alerts through the deployed IR Agent,
# measuring BOTH accuracy (completeness gate + verification score) AND speed
# (wall-clock time-to-resolution per alert). Writes one compact JSON per alert
# plus a summary row appended to speed_trend.tsv for the iteration trend table.
#
# Usage: ITER=<label> run_live_alerts.sh <RESULTS_DIR>
set -u
# Engine and repo root are derived, not hardcoded. The previous literals pointed at
# engine 1000000000000000003 (deleted — exists in neither project) and at a stale
# worktree path, which still existed, so runs silently read fixtures and wrote
# results to the wrong tree instead of failing.
PY="/c/python313/python"
USER_ID="first.last@acme.example"
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && { pwd -W 2>/dev/null || pwd; })}"
ENGINE_ID="${ENGINE_ID:-$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['remote_agent_runtime_id'])" "${REPO}/deployment_metadata.json")}"
ENGINE="https://us-central1-aiplatform.googleapis.com/v1beta1/${ENGINE_ID}"
ALERTS="${ALERTS_DIR:-${REPO}/tests/eval/alerts}"
RESULTS="${1:-${REPO}/tests/eval/results/speed}"
ITER="${ITER:-0}"
TREND="${REPO}/tests/eval/results/speed/speed_trend.tsv"
TMP="${RESULTS}/.tmp"
mkdir -p "$TMP" "$RESULTS" "$(dirname "$TREND")"
TOKEN="$(gcloud auth print-access-token)"

[ -s "$TREND" ] || printf "iter\talert\twall\treport\tsections\tverif\tcut\ttools\tcategory\n" > "$TREND"

run_once() {
  local aid="$1"; local msg="$2"
  local stream="${TMP}/stream-${aid}.txt"
  local res="${RESULTS}/live-${aid}.json"
  local sess_json session_id
  # Session creation is transiently flaky (esp. cold instances). Retry up to 5x
  # with backoff before giving up — a create miss must not be recorded as a
  # scenario result. A fresh token each attempt guards against token expiry.
  session_id=""
  local attempt
  for attempt in 1 2 3 4 5; do
    TOKEN="$(gcloud auth print-access-token)"
    sess_json=$(curl -s -m 30 -X POST "${ENGINE}:query" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
      -d "{\"class_method\":\"async_create_session\",\"input\":{\"user_id\":\"${USER_ID}\"}}")
    session_id=$(printf '%s' "$sess_json" | "$PY" -c "import sys,json;d=json.load(sys.stdin);print(d.get('output',{}).get('id') or '')" 2>/dev/null)
    [ -n "$session_id" ] && break
    echo "  ${aid}: session create attempt ${attempt} failed, retrying..."
    sleep 5
  done
  if [ -z "$session_id" ]; then printf '{"alert":"%s","error":"session"}\n' "$aid" > "$res"; return; fi

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
        for chunk in r: f.write(chunk)
except Exception as e:
    with open(stream_path,"ab") as f: f.write(("\n{\"__client_error__\":\""+str(e).replace(chr(34),"'")+"\"}\n").encode())
PYEOF
  end_t=$(date +%s); wall=$((end_t-start_t))

  ITER="$ITER" "$PY" - "$aid" "$stream" "$wall" "$res" "$TREND" <<'PYEOF'
import sys, json, os, re
aid, stream_path, wall, res_path, trend = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
it = os.environ.get("ITER","0")
events=[]
with open(stream_path,'r',encoding='utf-8',errors='replace') as f:
    for line in f:
        line=line.strip()
        if line.startswith('data:'): line=line[5:].strip()
        if not line: continue
        try: events.append(json.loads(line))
        except json.JSONDecodeError: continue
report_text=""; verif_text=""; classif_text=""; tools=0; max_tokens=0
for e in events:
    a=e.get('author') or ''
    um=e.get('usage_metadata',{}) or {}
    if um.get('total_token_count'): max_tokens=max(max_tokens,um['total_token_count'])
    for part in e.get('content',{}).get('parts',[]):
        if part.get('function_call'): tools+=1
        t=part.get('text')
        if t:
            if a=='report_agent' and len(t)>len(report_text): report_text=t
            if a=='verification_agent' and len(t)>len(verif_text): verif_text=t
            if a=='alert_classifier': classif_text=t
cut=any('UNAVAILABLE' in json.dumps(e) or '__client_error__' in e for e in events)
rt=report_text.lower()
# 5 checks over the four-section report (ADO-200018 removed Root Cause, Timeline and
# Recommended Actions; the causal claim moved into the first Key Findings bullet as
# "Mechanism:"). Scoring the removed sections would make the gate unsatisfiable.
# ADO-200020's **Links:** section is intentionally NOT scored here: it is correctly
# omitted when a run retrieves no verifiable URL, so a has_links key would fail an
# honest report. Asserted in tests/unit/test_report_format.py instead.
sections={"tldr":"**tl;dr:**" in rt,"sev":bool(re.search(r'sev|🟢|🟠|🔴',report_text)),
  "what":"what happened" in rt,"find":"key findings" in rt,"mech":"mechanism:" in rt}
sscore=sum(1 for v in sections.values() if v)
tele_captured=bool(max_tokens>0 and tools>0)
classif={}
try: classif=json.loads(classif_text)
except Exception: pass
res={"alert":aid,"iter":it,"wall_seconds":wall,"stream_cut":cut,"tools":tools,
  "total_tokens":max_tokens,"telemetry_captured":tele_captured,
  "report_present":bool(report_text),"report_sections_score":sscore,"verification_present":bool(verif_text),
  "category":classif.get("category"),"report_text":report_text[:4000],"verification_text":verif_text[:1500]}
json.dump(res,open(res_path,'w',encoding='utf-8'),indent=2)
with open(trend,'a',encoding='utf-8') as f:
    f.write(f"{it}\t{aid}\t{wall}\t{res['report_present']}\t{sscore}\t{res['verification_present']}\t{cut}\t{tools}\t{classif.get('category')}\n")
print(f"LIVE {aid} iter={it} wall={wall}s report={res['report_present']} sec={sscore}/5 tele={tele_captured} verif={res['verification_present']} cut={cut} cat={classif.get('category')}")
PYEOF
  rm -f "$stream"
}

# Retry-on-incomplete wrapper. An investigation is COMPLETE only with a full
# report (5/5 section checks, four-section format — ADO-200018), telemetry captured
# on the event stream, a verification score,
# and no stream cut. The two residual miss
# modes are transient (platform instance-recycle; deadline-edge on the heaviest
# scenario), so a fresh-session retry converts them to success. RETRIES = max
# attempts per alert (default 3). Each attempt is a full run; the result JSON is
# overwritten so the last (best) attempt is what's recorded.
is_complete() {
  "$PY" - "$1" <<'PYEOF'
import sys, json, os
p=sys.argv[1]
if not os.path.exists(p): sys.exit(1)
try: d=json.load(open(p,encoding='utf-8'))
except Exception: sys.exit(1)
ok=(d.get('report_present') and d.get('report_sections_score')==5
    and d.get('telemetry_captured')
    and d.get('verification_present') and not d.get('stream_cut'))
sys.exit(0 if ok else 1)
PYEOF
}

run_one() {
  local aid="$1"; local msg="$2"
  local res="${RESULTS}/live-${aid}.json"
  local max="${RETRIES:-3}" attempt=1
  while [ "$attempt" -le "$max" ]; do
    [ "$attempt" -gt 1 ] && echo "RETRY ${aid} attempt ${attempt}/${max} (previous incomplete)"
    run_once "$aid" "$msg"
    if is_complete "$res"; then
      "$PY" -c "import json;d=json.load(open('$res',encoding='utf-8'));d['attempts_used']=$attempt;json.dump(d,open('$res','w',encoding='utf-8'),indent=2)"
      [ "$attempt" -gt 1 ] && echo "RECOVERED ${aid} on attempt ${attempt}"
      return 0
    fi
    attempt=$((attempt+1))
  done
  echo "EXHAUSTED ${aid} after ${max} attempts"
  return 1
}

# Sequential: production handles one alert per invocation, and with
# concurrency=1 only a few instances are warm — running in parallel would queue
# and corrupt the per-alert time-to-resolution we are trying to measure. One at
# a time gives each alert a clean, warm, dedicated instance.
# PARALLEL=1 fires all alerts concurrently (each gets its own warm instance when
# min-instances >= alert count). Used for accuracy runs where speed is only a gate,
# not the metric, so co-tenancy latency noise is acceptable and wall-clock drops from
# sum-of-alerts to slowest-single-alert.
i=0
for f in "${ALERTS}"/alert_*.txt; do
  i=$((i+1)); aid="alert$(printf '%02d' "$i")"
  msg=$(tr -d '\r' < "$f" | tr '\n' ' ' | sed 's/"/\\"/g')
  if [ "${PARALLEL:-0}" = "1" ]; then
    run_one "$aid" "$msg" &
    sleep 8   # stagger starts so 5 simultaneous stream_query calls don't collide on
              # cold/half-warm instances (burst returned empty streams). Investigations
              # still overlap (each ~200s), so wall-clock stays near slowest-single.
  else
    run_one "$aid" "$msg"
  fi
done
[ "${PARALLEL:-0}" = "1" ] && wait
rmdir "$TMP" 2>/dev/null || true
echo "=== LIVE ALERTS ITER ${ITER} COMPLETE ==="
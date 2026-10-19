#!/usr/bin/env bash
# Concurrency wedge probe: reproduces the handoff's "wedge under concurrent load
# and never recovers" defect with hard evidence and timing.
#   1) single async_create_session (warm check)
#   2) N parallel async_create_session (the burst)
#   3) single async_create_session AFTER the burst (recovery check — the key regression)
# Prints HTTP code + time for each. HTTP 000 = hang/timeout (wedge).
# Usage: burst_probe.sh [N] [timeout_s]
set -u
# Derived, not hardcoded — see ADO-200017.
PY="${PY:-/c/python313/python}"
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && { pwd -W 2>/dev/null || pwd; })}"
ENGINE_ID="${ENGINE_ID:-$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['remote_agent_runtime_id'])" "${REPO}/deployment_metadata.json")}"
ENGINE="https://us-central1-aiplatform.googleapis.com/v1beta1/${ENGINE_ID}"
N="${1:-3}"
TMO="${2:-45}"
TOKEN="$(gcloud auth print-access-token)"
BODY='{"class_method":"async_create_session","input":{"user_id":"burst-probe@acme.example"}}'

req() {
  curl -s -m "$TMO" -X POST "${ENGINE}:query" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -d "$BODY" -w "%{http_code}/%{time_total}s" -o /dev/null
}

echo "=== 1. WARM single create (baseline) ==="
echo "  warm=$(req)"

echo "=== 2. BURST: ${N} parallel creates ==="
pids=(); i=0
while [ "$i" -lt "$N" ]; do
  ( echo "  burst[$1_$i]=$(req)" ) & pids+=($!)
  i=$((i+1))
done
for p in "${pids[@]}"; do wait "$p"; done

echo "=== 3. RECOVERY single create (post-burst — the key regression) ==="
echo "  recovery=$(req)"
echo "=== probe done ==="

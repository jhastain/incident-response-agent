#!/usr/bin/env bash
# Run every alert_*.txt in WORKDIR through the deployed IR Agent with CAPPED concurrency.
# Avoids the backend overload (503 / "MCP tools down") seen at full concurrency.
#
# Usage:  bash run_pool.sh <WORKDIR> [MAXJOBS] [STAGGER_SECONDS]
#   WORKDIR  dir containing alert_01.txt .. alert_NN.txt  (default: cwd)
#   MAXJOBS  max concurrent investigations          (default: 3)
#   STAGGER  seconds between launches               (default: 25)
#
# Each alert gets a FRESH gcloud token per call (long serial runs outlive a single token).
# Per-alert outputs: out_<base>.jsonl (raw SSE), meta_<base>.txt (status).
# Re-runnable: to retry a subset, delete/keep their .txt files or call run_one via a wrapper.
#
# ENGINE_URL is resolved LIVE (newest engine named incident_response_agent) WITHIN A
# PROJECT YOU MUST NAME. Override by exporting ENGINE_URL before calling.
#
# ADO-200021. The resolver used to pin projects/000000000002 (dev) inside this same
# block, while the comment claimed it "does not go stale on redeploy". That was true
# of the engine ID and false of the project: the search could not go stale within dev
# and could never leave it. A 10-alert batch run to certify a TEST deploy resolved
# every alert to the 2026-08-04 dev build — 10 sessions created, 10 reports streamed,
# normal timings, a rendered accuracy score, and the fix under test never exercised.
#
# So PROJECT is required and has no default: it selects the environment, and a wrong
# environment produces a complete, plausible, wrong result rather than an error.
# It is derived from deployment_metadata.json (same idiom as tests/eval/run_agent_eval.sh,
# ADO-200017), which records the last deploy FROM THIS CHECKOUT — correct when the
# checkout's last deploy matches the environment you mean to certify, which is exactly
# why the resolved target is echoed below before the first alert is sent.
set -u
# `${1:-${WORKDIR:-$PWD}}`, not `${1:-$PWD}`. The single-alert retry documented in
# ir-channel-batch-test/SKILL.md is `export WORKDIR=...; source run_pool.sh; run_one alert_03`,
# which passes no positional arg — so the old form CLOBBERED the exported WORKDIR with
# $PWD. run_one then looked for an alert file that was not there and still printed
# "FINISHED alert_03 elapsed=1s". A retry that cannot work must not report success.
WORKDIR="${1:-${WORKDIR:-$PWD}}"
MAXJOBS="${2:-3}"
STAGGER="${3:-25}"
USER_ID="${USER_ID:-first.last@acme.example}"

# Derived, not hardcoded — see ADO-200017. `pwd -W` because Windows python cannot open
# an MSYS /c/... path; plain `pwd` here cost a debugging cycle.
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && { pwd -W 2>/dev/null || pwd; })}"

if [ -z "${PROJECT:-}" ]; then
  PROJECT="$(python -c "import json,sys;print(json.load(open(sys.argv[1]))['remote_agent_runtime_id'].split('/')[1])" "${REPO}/deployment_metadata.json" 2>/dev/null)"
fi
if [ -z "${PROJECT:-}" ]; then
  echo "ERROR: cannot determine the target project. Export PROJECT=<project id or number>," >&2
  echo "       or ensure ${REPO}/deployment_metadata.json is readable." >&2
  exit 1
fi

if [ -z "${ENGINE_URL:-}" ]; then
  ENGINE_NAME="$(python - "$(gcloud auth print-access-token 2>/dev/null)" "$PROJECT" <<'PY'
import sys, json, urllib.request
token, project = sys.argv[1], sys.argv[2]
base = (f"https://us-central1-aiplatform.googleapis.com/v1/projects/{project}"
        "/locations/us-central1/reasoningEngines")
matches, page = [], ""
while True:
    url = base + "?pageSize=100" + (f"&pageToken={page}" if page else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    d = json.load(urllib.request.urlopen(req))
    matches += [e for e in d.get("reasoningEngines", [])
                if e.get("displayName") == "incident_response_agent"]
    page = d.get("nextPageToken") or ""
    if not page:
        break
if not matches:
    sys.exit(1)
matches.sort(key=lambda e: (e.get("updateTime", ""), e.get("createTime", "")), reverse=True)
sys.stderr.write(f"resolved engine updateTime={matches[0].get('updateTime','?')}\n")
print(matches[0]["name"])
PY
)"
  [ -z "$ENGINE_NAME" ] && { echo "ERROR: no engine named incident_response_agent in us-central1 of project ${PROJECT}"; exit 1; }
  ENGINE_URL="https://us-central1-aiplatform.googleapis.com/v1beta1/${ENGINE_NAME}"
fi

# State the target before the first alert. Both ADO-200021 and ADO-200017 were silent
# because nothing ever printed WHAT was being measured; a score is only traceable to
# the engine that produced it if that engine is recorded next to the results.
echo "project: ${PROJECT}"
echo "engine:  ${ENGINE_URL}"
echo "repo:    ${REPO}"
echo "workdir: ${WORKDIR}"
mkdir -p "$WORKDIR"
{
  printf 'project=%s\n' "$PROJECT"
  printf 'engine_url=%s\n' "$ENGINE_URL"
  printf 'engine_id=%s\n' "${ENGINE_URL##*/}"
  printf 'repo=%s\n' "$REPO"
  printf 'resolved_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$WORKDIR/target.txt"

run_one () {
  local base="$1"
  local f="$WORKDIR/${base}.txt" out="$WORKDIR/out_${base}.jsonl" meta="$WORKDIR/meta_${base}.txt"
  # Every early return below prints FAILED and returns non-zero. Previously they
  # returned silently and control fell through to the unconditional
  # "FINISHED $base elapsed=1s" at the end of the function, so a retry that never
  # reached the engine — missing alert file, expired token, refused session —
  # looked identical to a completed investigation in the pool's stdout.
  if [ ! -s "$f" ]; then
    echo "FAILED $base: no alert file at $f (WORKDIR=$WORKDIR)" >&2
    echo "NO_ALERT_FILE $f" >> "$meta"
    return 1
  fi
  : > "$out"; : > "$meta"
  echo "engine_url=$ENGINE_URL" >> "$meta"
  local TOKEN; TOKEN="$(gcloud auth print-access-token 2>/dev/null)"
  if [ -z "$TOKEN" ]; then
    echo "FAILED $base: no gcloud token" >&2
    echo "NO_TOKEN $base" >> "$meta"
    return 1
  fi
  local sresp sid
  sresp="$(curl -s -X POST "${ENGINE_URL}:query" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -d '{"class_method":"async_create_session","input":{"user_id":"'"${USER_ID}"'"}}')"
  sid="$(printf '%s' "$sresp" | python -c 'import sys,json;print(json.load(sys.stdin)["output"]["id"])' 2>/dev/null)"
  if [ -z "$sid" ]; then
    echo "FAILED $base: session creation refused :: ${sresp:0:200}" >&2
    echo "SESSION_FAIL $base :: $sresp" >> "$meta"
    return 1
  fi
  echo "session_id=$sid" >> "$meta"
  local payload="$WORKDIR/payload_${base}.json"
  python - "$f" "$sid" "$USER_ID" > "$payload" <<'PY'
import sys,json
msg=open(sys.argv[1],encoding="utf-8").read()
print(json.dumps({"class_method":"stream_query","input":{"user_id":sys.argv[3],"session_id":sys.argv[2],"message":msg}}))
PY
  TOKEN="$(gcloud auth print-access-token 2>/dev/null)"
  local s e; s="$(date +%s)"
  curl -s -N --max-time 1200 "${ENGINE_URL}:streamQuery?alt=sse" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -d @"$payload" > "$out" 2>&1
  e="$(date +%s)"
  local lines; lines="$(wc -l < "$out")"
  echo "elapsed_sec=$((e-s))" >> "$meta"; echo "lines=${lines}" >> "$meta"
  # An empty stream is a failure, not a fast success. The 1-second FINISHED line was
  # the only signal a broken retry ever emitted.
  if [ "$lines" -eq 0 ]; then
    echo "FAILED $base: empty stream after $((e-s))s (no SSE events)" >&2
    echo "EMPTY_STREAM $base" >> "$meta"
    return 1
  fi
  echo "DONE $base" >> "$meta"
  echo "FINISHED $base elapsed=$((e-s))s lines=${lines}"
}

# allow sourcing for single-alert retries: `source run_pool.sh; run_one alert_03`
# The resolution above has already run by this point, so a sourced retry hits the same
# project/engine as the batch and prints it — it does not silently pick its own.
[ "${BASH_SOURCE[0]}" != "${0}" ] && return 0

shopt -s nullglob
for f in "$WORKDIR"/alert_*.txt; do
  b="$(basename "$f" .txt)"
  while [ "$(jobs -rp | wc -l)" -ge "$MAXJOBS" ]; do sleep 3; done
  echo "LAUNCH $b"
  run_one "$b" &
  sleep "$STAGGER"
done
wait
echo "POOL_ALL_DONE"

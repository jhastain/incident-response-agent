#!/usr/bin/env bash
# Datadog telemetry/judge evidence campaign: run the 5 fresh alerts against the
# DEPLOYED IR Agent, cycled 6x each (30 runs total), with bounded concurrency.
#
# Concurrency: each investigation fans out ~16 sub-agents. tests/eval/ab_all.sh
# documented that running too many alerts at once saturates the local network
# and every run fails with SSL EOF on token fetches. Two alerts in flight (~64
# concurrent sub-agents) is the sustainable ceiling -- respected here via the
# same jobs-count polling pattern ab_all.sh uses.
#
# Resumable: a run is skipped if its result JSON already exists and is non-empty.
# A failed run is recorded (not retried, not fatal) so the campaign always
# finishes 30 attempts and reports the real failure count.
#
# Usage: dd_campaign.sh [max_parallel] [cycles]   (default 2 max_parallel, 6 cycles)
set -u
cd "$(dirname "$0")/../.." || exit 1

MAX="${1:-2}"
CYCLES="${2:-6}"
OUT_DIR="tests/eval/results/dd-campaign"
LOG="$OUT_DIR/campaign_log.tsv"
mkdir -p "$OUT_DIR"

if [ ! -f "$LOG" ]; then
  printf 'run_id\talert\tcycle\tstatus\twall_seconds\n' > "$LOG"
fi

run_one() {
  local alert="$1" cycle="$2"
  local stem run_id out t0 t1 status wall
  stem="$(basename "$alert" .txt)"
  run_id="${stem}_c${cycle}"
  out="$OUT_DIR/${run_id}.json"

  if [ -s "$out" ]; then
    echo "SKIP $run_id (already have result)"
    return
  fi

  echo "START $run_id"
  t0=$(date +%s)
  if uv run python tests/eval/deployed_run.py "$alert" "$out" > "$OUT_DIR/${run_id}.log" 2>&1; then
    status="ok"
  else
    status="failed"
  fi
  t1=$(date +%s)
  wall=$((t1 - t0))

  # A "successful" process exit can still carry an error field in the JSON
  # (deployed_run.py always exits 0 and records errors in-band) -- check that too.
  if [ "$status" = "ok" ] && [ -s "$out" ]; then
    if python -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('error') is None else 1)" "$out" 2>/dev/null; then
      status="ok"
    else
      status="failed"
    fi
  fi

  printf '%s\t%s\t%s\t%s\t%s\n' "$run_id" "$stem" "$cycle" "$status" "$wall" >> "$LOG"
  echo "DONE $run_id status=$status wall=${wall}s"
}

for cycle in $(seq 1 "$CYCLES"); do
  for f in tests/eval/alerts/alert_*.txt; do
    while [ "$(jobs -rp | wc -l)" -ge "$MAX" ]; do sleep 5; done
    run_one "$f" "$cycle" &
  done
done
wait

echo "=== CAMPAIGN COMPLETE ==="
total=$(($(wc -l < "$LOG") - 1))
ok=$(awk -F'\t' 'NR>1 && $4=="ok"{c++} END{print c+0}' "$LOG")
failed=$(awk -F'\t' 'NR>1 && $4=="failed"{c++} END{print c+0}' "$LOG")
echo "total=$total ok=$ok failed=$failed"

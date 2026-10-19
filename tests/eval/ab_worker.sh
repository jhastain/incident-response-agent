#!/usr/bin/env bash
# Run one alert through BOTH variants (local new-code, deployed old-code) in parallel.
# Usage: ab_worker.sh <alert_file>
# Writes .ab/<stem>.local.json and .ab/<stem>.deployed.json
set -u
cd "$(dirname "$0")/../.." || exit 1
alert="$1"
stem="$(basename "$alert" .txt)"
mkdir -p .ab

uv run python tests/eval/local_run.py    "$alert" ".ab/${stem}.local.json"    > ".ab/${stem}.local.log" 2>&1 &
pid_local=$!
uv run python tests/eval/deployed_run.py "$alert" ".ab/${stem}.deployed.json" > ".ab/${stem}.deployed.log" 2>&1 &
pid_dep=$!

wait $pid_local; rc_local=$?
wait $pid_dep;   rc_dep=$?
echo "DONE $stem local_rc=$rc_local deployed_rc=$rc_dep"

#!/usr/bin/env bash
# Run every ab-alert through both variants with BOUNDED concurrency.
#
# Concurrency matters: each investigation fans out ~16 sub-agents, so running all
# 11 alerts at once saturated the local network and every run failed with SSL EOF
# on oauth2.googleapis.com token fetches (and the deployed engine dropped its
# stream at ~91s). Two alerts in flight = 4 processes = ~64 concurrent sub-agents,
# which single-run probes show is sustainable.
#
# Usage: ab_all.sh [max_parallel_alerts]   (default 2)
set -u
cd "$(dirname "$0")/../.." || exit 1
MAX="${1:-2}"
mkdir -p .ab

for f in tests/eval/alerts/alert_*.txt; do
  stem="$(basename "$f" .txt)"
  # Skip pairs already complete (makes the script resumable after a failure).
  if [ -s ".ab/${stem}.local.json" ] && [ -s ".ab/${stem}.deployed.json" ]; then
    echo "SKIP $stem (already done)"
    continue
  fi
  while [ "$(jobs -rp | wc -l)" -ge "$MAX" ]; do sleep 5; done
  echo "START $stem"
  bash tests/eval/ab_worker.sh "$f" &
done
wait
echo "=== ALL RUNS COMPLETE ==="

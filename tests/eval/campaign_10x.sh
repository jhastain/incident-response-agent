#!/usr/bin/env bash
# 10-run concurrency campaign. Each iteration runs the 3-concurrent gate and
# appends a compact per-scenario summary to campaign_summary.tsv for pass-rate
# distribution + failure troubleshooting. Instances are warmed once up front
# (min-instances=3 keeps them warm between runs).
set -u
# Derived, not hardcoded — see ADO-200017.
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && { pwd -W 2>/dev/null || pwd; })}"
OUT="${REPO}/tests/eval/results/${CAMPAIGN_DIR:-campaign}"
SUMM="${OUT}/campaign_summary.tsv"
mkdir -p "$OUT"
N="${1:-10}"

if [ ! -s "$SUMM" ]; then
  printf "run\tscenario\treport\tsections\tcut\twall\ttools\treached_last\tdead\n" > "$SUMM"
fi

echo "=== warm instances ==="
bash "${REPO}/tests/eval/burst_probe.sh" 3 60 >/dev/null 2>&1 || true

for i in $(seq 1 "$N"); do
  label="run$(printf '%02d' "$i")"
  rdir="${OUT}/${label}"
  # resume: skip runs already recorded in the summary
  if grep -q "^${label}	" "$SUMM" 2>/dev/null; then
    echo "=== SKIP ${label} (already recorded) ==="
    continue
  fi
  echo "=== CAMPAIGN ${label} ($(date +%H:%M:%S)) ==="
  bash "${REPO}/tests/eval/run_agent_eval.sh" "$rdir" 2>&1 | grep -E "^DONE" || true

  # extract per-scenario diagnostics into the tsv
  /c/python313/python - "$label" "$rdir" "$SUMM" <<'PYEOF'
import sys, json, os
label, rdir, summ = sys.argv[1], sys.argv[2], sys.argv[3]
seq = ['alert_classifier','synthesis_agent','tier2_dispatcher','report_agent','verification_agent','persistence_agent']
scen = ['cloud-infrastructure-startup-probe','sql-database-preferences-blocking','application-errors-apigateway-mobile']
short = {'cloud-infrastructure-startup-probe':'cloud','sql-database-preferences-blocking':'sql','application-errors-apigateway-mobile':'app'}
rows = []
for sid in scen:
    p = os.path.join(rdir, f'eval-{sid}.json')
    if not os.path.exists(p):
        rows.append([label, short[sid], 'MISSING','0','?','?','?','none','?']); continue
    d = json.load(open(p, encoding='utf-8'))
    present = [a['author'] for a in d.get('per_agent',[])]
    reached = [a for a in seq if a in present]
    last = reached[-1] if reached else '(none)'
    dead = sum(a['tool_calls']-a['tool_responses'] for a in d.get('per_agent',[]) if a['tool_calls']!=a['tool_responses'])
    rows.append([
        label, short[sid],
        # 5 section checks over the four-section report (ADO-200018). The check set
        # is defined in run_agent_eval.sh, which writes report_sections_score.
        # Still 5 after ADO-200020: the restored **Links:** section is not scored,
        # because a run with no verifiable URL correctly omits it.
        'PASS' if (d.get('report_present') and d.get('report_sections_score')==5
                   and d.get('telemetry_captured') and d.get('verification_present')) else 'FAIL',
        str(d.get('report_sections_score','?')),
        str(d.get('stream_cut','?')),
        str(d.get('wall_clock_seconds','?')),
        str(d.get('total_tool_calls','?')),
        last,
        str(dead),
    ])
with open(summ,'a',encoding='utf-8') as f:
    for r in rows:
        f.write('\t'.join(r)+'\n')
for r in rows:
    print('  ', '\t'.join(r))
PYEOF
done
echo "=== CAMPAIGN COMPLETE — summary at ${SUMM} ==="

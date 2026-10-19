# Root-Cause Accuracy — Evidence-Coverage Gaps (flagged for future)

Established 2026-07-15 during the accuracy loop (branch ado-200001). Scored by an
independent Sonnet judge (`tests/eval/judge_from_files.py`) against the frozen rubric
(`tests/eval/accuracy-rubric.md`). All measurements server-side.

## The finding

The agent identifies the correct incident on every alert (10/10) and, after the iter1
fix, commits to a single mechanism instead of hedging (floor rose from 12 to 42). The
remaining ceiling is **evidence coverage**: for whole alert categories the mechanism-level
evidence (WHY it failed) is not reachable by the deployed agent, so the judge scores
`evidence_supported=False` and the rubric correctly caps the score at <=59.

This is a TOOL/CREDENTIAL gap, not a prompt gap. Verified live (not from memory):

| Category | Alerts | Mechanism evidence lives in | Reachable today? | What it would take |
|---|---|---|---|---|
| Octopus deploy hang | 01, 02 | Octopus task/step logs | ❌ No | Octopus API key provisioned as a GCP secret in aiops-dev-project + a direct Octopus task-log tool. No Octopus secret exists today. |
| ADO pipeline failure | 03, 04 | ADO Build timeline + failed-job logs | ❌ No | Direct ADO REST client using `codeagent-ado-pat` (verified working) mounted via deploy.ps1 --secrets + tools/ado_tools.py (Build list + Timeline + failed-step log). Bifrost `azure_devops_token` has NO pipeline tools (verified via live dir()). |
| App errors / 5xx | 05 | Datadog APM traces for the alert window | ⚠️ Partial | Datadog is already wired via Bifrost; needs the deploy_correlation/datadog prompt to pull the specific error-window traces (not just monitor status). Reachable now. |

## Verified evidence trail

- Bifrost `azure_devops_token` exposes exactly 10 tools (work-items + repo); zero
  `pipelines_*`. Confirmed via live Starlark `dir(azure_devops_token)`.
- Datadog CI Visibility (`datadog_token.search_datadog_ci_pipeline_events`) DOES have the
  ADO pipelines (7 Teardown-Env failures, status=error, ~4180s, branch main), but only at
  PIPELINE level — job-level and CI-log queries returned empty for the correct time window.
  So Datadog gives pipeline status/duration, NOT the failed-job mechanism.
- `codeagent-ado-pat` (GCP Secret Manager) authenticates to ADO Build REST APIs (verified:
  returned recent failed builds). This is the path if/when direct-ADO is approved.

## Consequence for the "every alert >=95" goal

The current fresh test set (from sre-alerts channel) is dominated by deploy/CI alerts (4 of
5), whose mechanism evidence is behind the gaps above. On that set the reachable ceiling is
~70-80 (strong correlation, no mechanism), not 95. To pursue 95 now, the test set must be
built from categories whose evidence IS Bifrost-reachable (app errors, SQL, cache, latency,
5xx). To pursue 95 on deploy/CI, wire the flagged tools/creds above.

## Iter1 fix that IS in place (helps every category)

Commit `9d6dd4d`: synthesis now commits to ONE mechanism (no co-equal-cause lists, no numeric
confidence, symptom!=cause, service-scope pivot); report carries the committed cause or a
best-supported hypothesis + named gap + next step. Raised fresh-set mean 38->44, min 12->42.

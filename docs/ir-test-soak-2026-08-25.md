# IR Agent test soak -- decision-tree routing (ADO-200023) and feedback loop (ADO-200024)

Environment: `aiops-test-project` (000000000001)
Engine: `projects/000000000001/locations/us-central1/reasoningEngines/1000000000000000001`
Report status: **INTERIM -- 0 of 5 real investigations observed.** Soak continues.
Written 2026-08-25 14:20 CDT. All observations read-only; no writes to Firestore, GCS, PagerDuty, ADO, or Teams.

## Headline

The deploy is live and healthy, and the routing logic is correct when replayed against
all 12 live tree documents. **But no real alert has arrived since the deploy**, so the
table below has zero rows. Separately, three defects were found by reading the shipped
code and probing the live environment -- two of which mean that even once alerts do
arrive, parts of your requested table are **not observable at all**.

## Investigation table

| # | Incident (PD id / number) | Category classified | Tier 1: ran / skipped (n of 9) | Agents skipped | Fail-open? (Y/N + reason) | Focus strings delivered? | Duration | Tree write-back (version before -> after, incident_count) | Report quality | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| -- | *(none)* | -- | -- | -- | -- | -- | -- | -- | -- | -- |

**Zero real investigations have completed since the deploy.** Not "none succeeded" --
none were triggered. Evidence below.

## Deploy confirmation

| Fact | Value | Source |
|---|---|---|
| Engine ID | `.../reasoningEngines/1000000000000000001` (unchanged) | `gcloud run services describe ir-pagerduty-receiver` -> `IR_AGENT_ENGINE_URL`, 13:30 CDT |
| Last engine operation | `6749082218473193472`, done=true, no error | reasoningEngines/operations REST, 13:33 CDT |
| Deploy window | 2026-08-25T18:13:22Z -> **18:23:17Z** (13:13 -> 13:23 CDT) | same |
| Engine `updateTime` | 2026-08-25T18:23:17.748291Z | reasoningEngines GET, 13:31 CDT |
| Container startup | clean; uvicorn up 18:24:00Z, no tracebacks | `reasoning_engine_stderr`, 13:36 CDT |
| Receiver revision | `ir-pagerduty-receiver-00003-8x4`, 100% traffic | `gcloud run services describe`, 13:30 CDT |

**Counting cutoff: 18:23:17Z.** The receiver was not part of this deploy (last modified
2026-08-06); only the Agent Engine changed.

## Why there are no rows

| Fact | Value | Source |
|---|---|---|
| Newest incident in Firestore | `20260825-164506-application-errors` @ 16:45:06Z | Firestore `runQuery` on `incidents`, 13:34 CDT and re-checked 14:18 CDT |
| That incident vs cutoff | **1h38m BEFORE** the deploy finished -- correctly excluded | -- |
| Newest PD incident, team PTEAMSR | #1002 `QEXAMPLE000001` @ 16:40:18Z | `mcp__pagerduty__list_incidents`, 14:10 CDT |
| PD incidents since 18:00Z | **0** | `mcp__pagerduty__list_incidents` since=18:00Z, 14:09 CDT |
| Webhook deliveries since deploy | **0** (last `POST /pagerduty/webhook 202` at 16:40:19Z) | receiver Cloud Run logs, 14:15 CDT |

The pipeline itself is healthy -- the 16:40 alert shows the full chain working:
webhook 202 -> `investigating incident QEXAMPLE000001` -> two POSTs to the engine ->
`note posted=True (agent ok=True)`. Nothing is broken; nothing has paged.

**Measured arrival rate** (last 80 incidents): 08-25=4, 08-24=4, 08-23=1, 08-22=1,
08-21=5, 08-20=22, 08-19=20, 08-18=23. At the recent ~4/day rate, five real
investigations is a multi-day wait.

## Baseline tree state (recorded 13:38 CDT, re-verified unchanged 14:18 CDT)

All 12 documents: `incident_count=0`, `optimization_log` empty, `applied_incident_ids`
empty, none pinned. **109 triage steps total, zero `teams_context` steps**,
`monitoring-meta` at version 2 -- matches the expected seed.

| Category | version | steps (tier-1) | hit rates |
|---|---|---|---|
| application-errors | 1 | 9 (8) | all 0.5 |
| content-archive | 1 | 9 (8) | 0.4-1.0 (hand-tuned) |
| auth-security | 1 | 9 (8) | all 0.5 |
| business-logic | 1 | 9 (8) | all 0.5 |
| cloud-infrastructure | 1 | 8 (8) | all 0.5 |
| data-integrity | 1 | 10 (8) | all 0.5 |
| deploy-release | 1 | 9 (8) | all 0.5 |
| monitoring-meta | 2 | 9 (8) | all 0.5 |
| performance | 1 | 10 (8) | all 0.5 |
| pubsub-messaging | 1 | 9 (8) | all 0.5 |
| scheduled-jobs | 1 | 9 (8) | all 0.5 |
| sql-database | 1 | 9 (8) | all 0.5 |

Raw copies: `baseline_trees_raw.json`, `baseline_trees.json` in the session scratchpad.

## Routing effectiveness

**No live evidence.** Zero investigations ran, so the adversarial question -- did
narrowing lose evidence a skipped agent held -- **cannot be answered yet**. I will not
grade it from a simulation.

What I did verify, by replaying `build_tier1_plan` against each of the 12 live tree
documents (13:52 CDT):

| Category | selected | skipped | fail_open | focus strings |
|---|---|---|---|---|
| application-errors | 6 | compute_engine_health, gcp_metrics, teams_context | N | 6 |
| content-archive | 3 | cloud_run, compute_engine, gcp_logging, gcp_metrics, sentry, teams | N | 3 |
| auth-security | 6 | compute_engine_health, gcp_metrics, teams_context | N | 6 |
| business-logic | 5 | cloud_run, compute_engine, gcp_metrics, teams | N | 5 |
| cloud-infrastructure | 6 | sentry_error, teams_context, known_issues | N | 6 |
| data-integrity | 5 | cloud_run, compute_engine, gcp_metrics, teams | N | 5 |
| deploy-release | 6 | compute_engine_health, gcp_metrics, teams_context | N | 6 |
| monitoring-meta | 7 | compute_engine_health, teams_context | N | 7 |
| performance | 6 | compute_engine, teams, known_issues | N | 6 |
| pubsub-messaging | 6 | compute_engine, teams, known_issues | N | 6 |
| scheduled-jobs | 6 | cloud_run_health, gcp_metrics, teams_context | N | 6 |
| sql-database | 6 | cloud_run, compute_engine, teams | N | 6 |

Every category: `fail_open=False`, `tree_source="tree"`, a focus string for every
selected agent. `teams_context` skipped in all 12.

**Important caveat on what is actually being tested.** No live tree prunes anything --
11 of 12 list every agent at hit_rate 0.5. Selection is
`DEFAULT_TIER1_SELECTION` (in-repo, hand-authored) INTERSECTED with the tree's tier-1
steps. Since the tree never narrows the intersection, **today the Firestore document
contributes step ORDER and FOCUS TEXT, not narrowing**. The narrowing under test is the
hardcoded table. Source: `app/tier1_routing.py`, module docstring and
`DEFAULT_TIER1_SELECTION`.

## Feedback loop

Baseline vs final: **identical** -- no category saw an incident, so nothing moved.
Re-read at 14:18 CDT confirms all 12 still at `incident_count=0`, empty
`optimization_log`, empty `applied_incident_ids`.

**`MIN_SAMPLES` gating did not hold, because it does not exist in the runtime path.**
The only occurrence in the repo is `scripts/mine_decision_tree_hit_rates.py:48`
(`MIN_SAMPLES = 20`), an offline miner. `record_incident_contributions` applies every
usable incident immediately with no sample floor; the only damping is `PRIOR_WEIGHT=10`,
capping one incident's movement at ~0.045. This is a factual difference from the premise
of the question, not a pass or a fail.

## Defects found

### D1 -- The routing note is not observable in Cloud Logging (blocks the deliverable)

`tree_router` logs the success path at INFO (`logger.info("Tier 1 routing: %s", note)`,
`app/sub_agents/tree_router.py`). The deployed engine's root log level is WARNING, so
**no app-module INFO record has ever reached Cloud Logging.**

Evidence (14:00 CDT):
- Every `LEVEL:logger` prefix in `reasoning_engine_stderr` over 2026-08-18..08-25 is
  third-party: 34 `ERROR:asyncio`, 32 `WARNING:google_adk...`, 3 `WARNING:ddtrace...`,
  1 `WARNING:opentelemetry.trace`, 1 `ERROR:ddtrace.llmobs`. Zero from any `app.*` logger.
- Direct control: `"Persistence complete"` -- a `logger.info` on every successful
  incident -- has **zero** hits since 2026-08-15 despite dozens of persisted incidents.
- No `logging.basicConfig` anywhere in the agent (the only one in the repo is
  `integrations/pagerduty/main.py:26`, a different service).
- No `LOG_LEVEL` env var on the engine deployment spec (15 env vars listed; none set it).

Consequence: a **FAIL-OPEN is visible** (`logger.warning`, above threshold) but a
**normal routing decision is not**. And `tier1_plan` / `tier1_routing_note` are session
state that persistence never writes -- it stores `findings`, `synthesis`, `report`,
`verification`, `telemetry`, but not the plan.

Workaround for the remaining soak: the skip sentinel *is* persisted, inside
`incident.findings.findings_<agent>`, beginning
`NOT RUN -- skipped by decision tree '<cat>' v<N>: <reason>`. Ran/skipped, tree version,
and per-agent reason are reconstructable from the stored document. **Focus strings are
not recoverable this way** and will be reported as unknown.

### D2 -- `report.py` has an empty section header and never learned what a sentinel means

`app/sub_agents/report.py:55` is `**Optional (from the tree router):**` followed by a
blank line and then the next `##` heading. The ADO-200023 diff added exactly those two
lines and nothing beneath them -- the prompt announces a state source and lists none.

Worse, PR 110803's description claims *"synthesis.py / report.py -- both prompts told a
`NOT RUN` finding means the source was never queried"*. That is true of `synthesis.py`
(1 occurrence plus a ~15-line section) and **false of `report.py`**: `grep -c "NOT RUN"
report.py` = 0, and no "never queried", "routed out", or "not checked" either. The only
other 287187 hunk in `report.py` instructs it *not* to render routing.

So the report agent receives sentinel-bearing `{findings_*}` variables with zero
instruction on what a sentinel means. That is precisely the "NOT RUN misread as clean"
failure mode the evaluation asks about, and it is unguarded at the report layer.
Synthesis is guarded; report is not.

### D3 -- The feedback loop penalises an agent for being routed out

A skipped agent is scored a **miss**, not an omission, pushing its hit rate down for
never having run.

Mechanism, verified by executing the shipped pure functions (13:47 CDT):
1. `skip_sentinel()` output contains no backtick span, URL, or id-shaped token, so
   `artifact_tokens(sentinel)` returns `set()` -- confirmed empty.
2. `score_contributions()` omits an agent only when its findings key is **absent**. But
   `parallel.py`'s gate sets `state[findings_key] = sentinel`, so the key is present and
   non-blank, and persistence copies it through (its filter is `if state.get(k)`, which
   a sentinel passes).
3. Result: `score_contributions` returns `teams_context_agent: False` -> `(0 hits,
   1 sample)` -> `posterior_hit_rate(0,1) = 0.4545`.

Self-reinforcing: skipped agents look useless, which argues for skipping them again.
`teams_context` is skipped in all 12 categories, so it takes this hit on every incident.
The `PRIOR_WEIGHT=10` damping slows it but does not correct the sign.

## What I could not verify

- **Anything requiring a real investigation** -- routing effectiveness, evidence loss
  from narrowing, focus strings actually reaching sub-agent prompts, tree write-back,
  version bumps, `optimization_log` entries, duration, report quality. Zero alerts have
  arrived since the deploy.
- **Focus-string delivery**, even once alerts arrive -- not logged (D1) and not
  persisted. Would need trace inspection or a code change.
- **Whether the deployed image contains the merged code.** `sourceCodeSpec` returns
  `{"inlineSource": {}, "imageSpec": {}}` with no digest or commit. The deploy operation
  succeeded 4 min after the PRs merged (17:18Z merge, 18:13Z deploy start), which is
  consistent, but I did not confirm the running bytes.
- **D3 end-to-end.** Reproduced against the shipped functions in isolation; not yet
  observed folding a real sentinel into a real tree document.
- **PD webhook subscription `PSUBSCR` config.** The PagerDuty MCP exposes no
  subscription tool; I verified inflow from receiver logs instead.

# IR Agent — Tool Determinism Analysis

**Date:** 2026-07-14 · Evidence: deep_timing event log (`tests/eval/results/speed/deep_fullhaiku.json`), agent source.
**Question:** Can we make tool calls deterministic — a sub-agent builds a query package, then a deterministic fetch function runs each tool?

## What each agent actually does (observed, one warm run)

| Agent | Tools called | Non-deterministic part |
|---|---|---|
| infra_context | get_service_context ×1, load_infra_context ×2 | **Mostly deterministic** — looks up static service config by name |
| cloud_run_health | list_cloud_run_services, describe_cloud_run_service, list_cloud_run_revisions | LLM picks which service/region to describe |
| gcp_logging | query_gcp_logs ×6 | **LLM writes 6 different log filter queries** |
| gcp_metrics | list_alert_policies, query_time_series ×5 | **LLM picks 5 metrics + windows** |
| datadog_observability | listToolFiles ×1, getToolDocs ×3, executeToolCode ×7 | **Bifrost meta-tool dance** + LLM writes each DD query |
| deploy_correlation | listToolFiles, getToolDocs ×3, executeToolCode ×6, list_cloud_run_revisions ×2 | same meta-tool dance + query authoring |
| sentry_error | listToolFiles, getToolDocs ×2, executeToolCode ×4 | meta-tool dance + query authoring |
| known_issues | listToolFiles, getToolDocs ×4, executeToolCode ×4 | meta-tool dance + WIQL authoring |
| teams_context | listToolFiles ×1 | trivial |
| historical_context | find_similar_incidents, get_triage_guidance, list_incident_files, read_incident_file ×2 | **Mostly deterministic** — category-keyed lookups |

## The two sources of non-determinism (and time)

### 1. The Bifrost meta-tool "dance" (pure overhead, ~30-40% of Bifrost-agent time)
Bifrost exposes only 4 META-tools. To call a real downstream tool (e.g. Datadog `search_datadog_logs`), an agent must:
1. `listToolFiles` — discover available tools (~1-20s, one round-trip)
2. `getToolDocs` ×N — learn each tool's params (~1-3s each)
3. `executeToolCode` — finally run the query

Observed for datadog: `listToolFiles`(43.7→45) → 3× `getToolDocs`(66→69, note the **20s gap** 45→66 = LLM thinking) → 7× `executeToolCode`. **~3-4 of ~11 calls are discovery, not data.** This is per-agent, every run. The 5 Bifrost agents (datadog, deploy, sentry, known_issues, teams) all pay it.

### 2. LLM query authoring (the actual reasoning — genuinely variable)
Each agent's LLM writes the specific queries: which log filters, which metrics, which time windows, which DD/Sentry/ADO search terms — derived from the alert. This is real per-incident reasoning, and it's WHY tool count/latency varies run-to-run (63s–384s Tier-1 for the same alert).

## Can we make it deterministic? — Yes, in three tiers

### Tier A — Eliminate the Bifrost meta-tool dance (HIGH value, LOW risk, ~30-40% of Bifrost time)
The 4 Bifrost meta-tools and the downstream tool schemas are **static** — they never change per incident. Yet every agent re-discovers them via listToolFiles/getToolDocs every run.
**Fix:** pre-bake the downstream tool signatures into each agent's prompt (or wrap the specific downstream tools as first-class ADK FunctionTools that call Bifrost `executeToolCode` directly with known params). Agents skip listToolFiles/getToolDocs entirely → straight to executeToolCode. Removes ~3-4 calls/agent + the LLM "which tool?" thinking gaps.
Prior attempt (fde96e0) tried this via prompt and BACKFIRED (removed the governor → more calls). The RIGHT version is code: register the downstream tools as real FunctionTools so the model can't do discovery at all. No prompt persuasion needed.

### Tier B — Deterministic query package + fetch (YOUR IDEA — MEDIUM value, MEDIUM risk)
Structure: one "planner" sub-agent reads the classification + alert and emits a STRUCTURED query package (JSON): {datadog:[queries], gcp_logs:[filters], metrics:[names+windows], sentry:[searches], ado:[wiql]} — all anchored on the incident timestamp/service. Then a **deterministic Python fetch layer** executes every query concurrently (no LLM per call), returns raw results, and ONE summarizer LLM per domain condenses them.
- **Pro:** collapses 9 Tier-1 agents (each an LLM loop) into 1 planner + N parallel deterministic fetches + 1 summarizer. Removes the per-agent LLM tool-authoring loops (the variance source). Fetches parallelize freely (Bifrost handles concurrency — proven 9/9).
- **Con:** the planner must author good queries in one shot (no iterative refine); loses the "if empty, try alternative" adaptivity each agent currently has. Accuracy must be gated.
- This is essentially replacing the ReAct loop with plan→fetch→summarize. Legit pattern.

### Tier C — Fully static queries per category (LOW value for accuracy, HIGH determinism)
For each category, hard-code the exact query set (the decision-tree seeds already gesture at this). Zero LLM in the fetch path. Fastest + fully deterministic, but least adaptive — a query set that's wrong for an unusual incident returns nothing and the agent can't recover. Violates the "accuracy paramount" constraint for edge incidents.

## Recommendation (ordered)

1. **Tier A first** — pure win: register downstream Bifrost tools as real FunctionTools so agents skip discovery. No accuracy cost (same data calls), removes ~30-40% of Bifrost-agent latency + the discovery thinking-gaps. This is the safe, large lever.
2. **Then Tier B** (your query-package idea) as a bigger refactor, accuracy-gated against the 5 live alerts — this is what could actually break <180s, by removing the 9 per-agent LLM loops.
3. **Avoid Tier C** except as a per-category FAST PATH fallback, never the only path.

## Why this is the right direction for <180s
The timing floor (~266s spine) is dominated by Tier-1 (76s) + tier2 (54s) + the LLM generations. Tier A trims Bifrost-agent time; Tier B removes the per-agent LLM loops entirely and lets deterministic fetches parallelize — the only structural change that attacks the investigation-phase floor without cutting evidence.

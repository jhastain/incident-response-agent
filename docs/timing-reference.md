# IR Agent — E2E Timing Reference

**Captured:** 2026-07-14 · **Build:** full-Haiku (commit `9620b9e`) · **Scenario:** alert03 (SQL/deploy, live SRE-Alert), warm instance
**Engine:** `reasoningEngines/1000000000000000003` (aiops-dev-project, us-central1)
**Tool:** `tests/eval/deep_timing.py` (records every event's wall-clock arrival + author + tool call)

> **Note (ported to AIOps 2026-07-29):** the engine ID below (`1000000000000000003`) was current on the capture date and has since been replaced. Resolve the live engine by `displayName: incident_response_agent` — see the `investigate` skill.

## Summary

| Build | Mean wall (5 live alerts) | Best warm | Reports |
|---|---|---|---|
| Sonnet baseline | ~601s | ~536s | 3/4 |
| + per-agent MCP sessions + Haiku Tier-1 | ~573s | 533s | mixed |
| + code-only persistence | ~452s | 393s | 4/5 |
| **full-Haiku (synthesis+report Haiku, verification Sonnet)** | **~377s** | **299s** | **5/5** |

591s → 299s best warm (−49%); ~377s mean. Accuracy verified equal to Sonnet (same classification, same verification tier MEDIUM, equivalent root-cause quality; 5/5 vs 4/5 completeness).

## Sequential pipeline — per stage (warm, 298.8s total)

| # | Stage | Model | Start | End | Duration | % | Type |
|---|---|---|---|---|---|---|---|
| 1 | alert_classifier | Sonnet | 6.4 | 6.4 | ~2s | <1% | classify |
| 2 | infra_context | Sonnet | 8.8 | 41.6 | 33s | 11% | sequential prelude (gates Tier-1) |
| 3 | tier1 (9 parallel) | Haiku | 43.0 | 119.3 | 76s | 26% | investigation |
| 4 | synthesis | Haiku | ~119 | 164.0 | ~45s | 15% | generation |
| 5 | tier2 (dispatcher+specialist) | Haiku | 184.7 | 239.1 | 54s | 18% | investigation |
| 6 | report | Haiku | ~239 | 268.8 | ~30s | 10% | generation |
| 7 | verification | Sonnet | 270.5 | 298.4 | 28s | 9% | scoring |
| 8 | persistence | code (no LLM) | 298.4 | ~299 | ~1s | <1% | write GCS+Firestore |
| | **TOTAL** | | | | **298.8s** | | |

Notes:
- Stages 4 and 6 emit a single event at completion, so the "gap" before each IS its generation time.
- Sequential spine floor ≈ infra 33 + tier1 76 + synthesis 45 + tier2 54 + report 30 + verif 28 ≈ 266s. Most is real investigation + generation.

## Tier-1 parallel agents (stage 3) — run concurrently

| Agent | Model | Start | End | Span | Tool calls |
|---|---|---|---|---|---|
| deploy_correlation | Haiku | 43.8 | 119.3 | 75.5 (barrier) | 12 |
| datadog_observability | Haiku | 43.7 | 117.3 | 73.6 | 11 |
| sentry_error | Haiku | 43.3 | 113.6 | 70.3 | 7 |
| known_issues | Haiku | 43.5 | 108.8 | 65.3 | 9 |
| historical_context | Sonnet | 44.7 | 102.4 | 57.7 | 5 |
| gcp_logging | Haiku | 47.1 | 98.5 | 51.4 | 6 |
| cloud_run_health | Haiku | 44.0 | 88.3 | 44.3 | 3 |
| gcp_metrics | Haiku | 45.8 | 88.0 | 42.2 | 6 |
| teams_context | Haiku | 43.0 | 66.7 | 23.7 | 1 |
| compute_engine_health | Haiku | 66.9 | 66.9 | 0 | 0 |

Tier-1 phase = slowest agent (deploy_correlation 75.5s), NOT the sum — agents genuinely parallelize (all start ~43-47s) after the per-agent-MCP-session fix (`72982c6`).

## Historical run-to-run variance (SAME build/code)

Tier-1 span observed: 63s / 76s / 138s / 384s / 551s across runs with NO errors = Bifrost/Vertex tool-call latency variance. This is the dominant remaining wildcard and is infrastructure, not repo code.

## Key commits (speed work, branch ado-200001, not pushed)

- `72982c6` per-agent MCP sessions (unblock Tier-1 parallelism)
- `11856ae` fix Haiku model id (`claude-haiku-4-5`, not `-20251001` which 404s on Vertex)
- `29c1ea5` code-only persistence (128s LLM → ~1s)
- `9620b9e` synthesis+report on Haiku (verification kept Sonnet)

## What's NOT reached
<180s. Best warm 299s. The sequential spine (~266s of real work) is the floor. Sub-180s needs either collapsing a stage (merge synthesis+report, or move verification off the critical path) or reducing tool-call latency/count — see determinism analysis in `docs/tool-determinism-analysis.md`.

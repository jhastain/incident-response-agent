# IR Agent — Accuracy Audit

Read-only audit of `<workspace>/aiops/agents/incident_response_agent/` (excl. `.venv`), branch
`ado-verifier-truncation`, working tree at the time of audit (uncommitted changes present in
`app/sub_agents/verification.py` and `app/tools/session_tools.py` — both are read as they stand).

Every claim below carries a `file:line` citation. Statements I could not verify from source are
labelled **INFERENCE**.

---

## Deliverable A — Inventory of accuracy-limiting constraints

### A.1 Prompt-level tool-call budgets

| # | Location | Value | Stated justification (verbatim from source) | Accuracy cost |
|---|---|---|---|---|
| A1 | `app/shared_prompts.py:57` | "**Maximum 6 tool calls total per investigation.**" | "These rules prevent token overflow. Violating them will crash the pipeline." (`shared_prompts.py:55`) | **High.** Applies to every agent built via `build_agent_instruction` (`shared_prompts.py:282` injects `TOKEN_BUDGET_RULES` unconditionally). Callers: `tier1/compute_engine_health.py:19`, `tier1/teams_context.py:20`, `tier2/sql_dba.py:33`, `tier2/cache.py:30`, `tier2/codebase.py:25`, `tier2/auth_security.py:27`, `tier2/pubsub.py:22`. The SQL DBA agent has a **9-item checklist** (`tier2/sql_dba.py:15-27`) and a 6-call budget — it is arithmetically impossible to complete the checklist. Same for cache (7 items, `tier2/cache.py:14-24`) and auth_security (6 items + discovery overhead, `tier2/auth_security.py:14-21`). |
| A2 | `app/shared_prompts.py:73` | "at most 1 listToolFiles + 1 getToolDocs + 4 executeToolCode = 6 calls" | Same block header. | **High.** Only **4** actual data calls reach Datadog/ADO for any Bifrost-mediated tier-2 agent. Note `readToolFile` is already filtered out at the toolset level (`tools/bifrost_mcp.py:229`), so 2 of the 6 are pure discovery tax. |
| A3 | `app/shared_prompts.py:71` | "Keep your final output under **1,500 words**." | Token-overflow prevention (same block). | Medium. Compresses tier-2 findings before synthesis ever sees them. |
| A4 | `app/shared_prompts.py:24` (`INVESTIGATION_RULES` rule 13) | "If investigation hits a dead end after **3 attempts**, stop and report what was checked." | None stated. | Medium. Applies to every agent (`shared_prompts.py:280`). No definition of "attempt" — **INFERENCE**: a model will read this conservatively and stop early. |
| A5 | `app/shared_prompts.py:57` | "Do NOT repeat failed calls more than once." | Same block. | Medium. A transient Bifrost 5xx (the exact failure mode `tools/tool_resilience.py:5-24` documents as common under parallel fan-out) burns the only retry. `tool_resilience.py:54` reinforces: "Do NOT retry it more than once." |
| A6 | `app/sub_agents/synthesis.py:159` | "Tool-call budget: at most **3** targeted follow-up calls." | "This bound exists because synthesis runs on the critical path before report generation — over-investigating here starves the report of its time budget." (`synthesis.py:160-162`) | **High.** Synthesis is the only stage that sees all 9 tier-1 findings side by side and is therefore the only stage positioned to close a cross-source gap. It is capped at 3 calls, of which at most 1 may chase the deeper trigger (A7). |
| A7 | `app/sub_agents/synthesis.py:153` | "Make **AT MOST ONE** such escalation call. If it returns nothing, report the trigger as an explicit named gap — do NOT then speculate." | Implicit in A6's time-budget rationale. | **High.** This is the single mechanism in the whole pipeline for going from proximate cause → true trigger, and it gets one shot. The prompt then forbids a second attempt: "One honest escalation attempt, then commit or name the gap" (`synthesis.py:154`). |
| A8 | `app/sub_agents/synthesis.py:157-158` | "If the Tier 1 findings already support a root cause from 2+ sources, make **ZERO** tool calls" | Same time-budget rationale. | Medium-High. "2+ sources agree" is a weak bar for stopping — two tier-1 agents can agree on a *symptom*. |
| A9 | `app/sub_agents/verification.py:301` | "**1-3 tool calls maximum** (get_session_tool_outputs)" | Header "## Token Budget". | Low. There is only one tool; 1 call is the natural number. Effectively inert. |
| A10 | `app/sub_agents/historical_context.py:74` | "Maximum **4 tool calls** (2 Firestore + optional 2 GCS fallback)" | Header "## Token Budget". | Low-Medium. Caps how many past incidents can be read. |
| A11 | `app/sub_agents/infra_context.py:67-68` | "Maximum **4 tool calls**", "Keep output under **800 words**" | Header "## Token Budget". | Medium. `service_context` is consumed by every tier-1 agent (`datadog_tierb.py:413`, `gcp_logging_tierb.py:63`, `gcp_metrics_tierb.py:103`, `cloud_run_health_tierb.py:77`, `sentry_tierb.py:50`, `deploy_correlation_tierb.py:64`). An 800-word ceiling on the shared substrate propagates to all 9. |
| A12 | `app/sub_agents/tier2_dispatcher.py:63` | "Extra dispatches waste the run's limited time budget and can cause the final report to be cut off before it is produced." | Explicit time-budget rationale. | **High** — see B.2. This converts a soft matrix into a hard prohibition on evidence-driven dispatch. |

### A.2 Per-agent query-plan caps (soft, prompt-level)

| # | Location | Value | Accuracy cost |
|---|---|---|---|
| A13 | `tier1/datadog_tierb.py:521-522` | "Plan **3-6 queries** … Do NOT over-query." | Medium. |
| A14 | `tier1/gcp_logging_tierb.py:94` | "**2-5 queries** is typical; only add what the alert justifies." | Medium. |
| A15 | `tier1/gcp_metrics_tierb.py:182-183` | "1 list_alert_policies … then **2-5** query_time_series … Do NOT over-query." | Medium. |
| A16 | `tier1/sentry_tierb.py:103-106` | "Plan **3-5 queries** total … Do NOT over-query." | Medium. |
| A17 | `tier1/deploy_correlation_tierb.py:119-120` | "Plan **3-5 queries** … Do NOT over-query." | Medium. |
| A18 | `tier1/sentry_tierb.py:99` | "always pass **limit=5** to search_issues" | Medium. Justified by "164K events/day across all projects" — a real volume argument, not purely a speed one. But `sort="freq"` + limit=5 means a low-volume, window-correlated issue (exactly what `synthesis.py:63-73` says the root cause usually is) can be invisible. |
| A19 | `tier1/sentry_tierb.py:105` | "get_sentry_resource for **at most the top 2-3** issue IDs" | Medium. |
| A20 | `tier1/deploy_correlation_tierb.py:100`, `:109` | `limit=10` on `search_datadog_ci_pipeline_events` | Low. |

### A.3 Hard code-level caps (these truncate real data)

| # | Location | Value | Stated justification | Accuracy cost / removal risk |
|---|---|---|---|---|
| A21 | `app/tools/bifrost_mcp.py:34` | `MAX_TOOL_RESPONSE_CHARS = 4_000` | "Prevents token overflow (observed: 1.36M tokens > 1M max)" (`bifrost_mcp.py:122-123`) | **High.** Applied via `after_tool_callback=truncate_tool_response` on: `synthesis.py:312`, `tier1/teams_context.py:41`, `tier1/compute_engine_health.py:50`, `tier2/sql_dba.py:109`, `tier2/cache.py:92`, `tier2/codebase.py:61`, `tier2/auth_security.py:58`, `tier2/pubsub.py:44`. 4,000 chars is roughly one Datadog monitor object. Removing it (i) improves accuracy but (ii) genuinely risks context overflow — the 1.36M-token observation is cited in-source. **Recommendation: raise, don't remove.** |
| A22 | `app/tools/bifrost_direct.py:70` | `[:6000]` on every executeToolCode result | None stated. | **Very high.** This is the fetch path for the LIVE tier-1 agents — `datadog_tierb.py:31`, `sentry_tierb.py:18`, `known_issues_tierb.py:17`, `deploy_correlation_tierb.py:24` all call `bifrost_execute_many`. Every Datadog/Sentry/ADO payload in the primary investigation path is cut at 6,000 chars with **no truncation marker emitted** — the caller cannot tell a complete result from a cut one. This directly contradicts `shared_prompts.py:116` ("Truncated means partial") and `datadog_tierb.py:539-543` ("if a result carries `is_truncated: true` … report it as 'showing X of Y'"), because the slice happens *below* the layer that would carry the flag. Removing raises overflow risk; **adding a marker costs nothing and should be done regardless.** |
| A23 | `app/tools/bifrost_direct.py:72-73` | `[:500]` on errors, `[:2000]` on unparsed responses | None stated. | Medium. A 500-char cut can remove the actionable part of a Bifrost error. |
| A24 | `app/tools/gcp_tools.py:15` | `_MAX_OUTPUT_CHARS = 4_000` | "Truncate tool output to stay within token budget" (`gcp_tools.py:87`) | **High.** Applied to `query_gcp_logs` (`:137`), `query_time_series` (`:208`), `list_alert_policies` (`:249`), `list_cloud_run_services` (`:286`), `list_cloud_run_revisions` (`:335`), `describe_cloud_run_service` (`:388`). A 20-entry error-log query with stack traces exceeds 4,000 chars easily — so the default log scan is routinely cut mid-evidence. |
| A25 | `app/tools/gcp_tools.py:94` | `query_gcp_logs(limit: int = 20)` | Docstring only. | Medium. `gcp_logging_tierb.py:76` tells the model it may raise it, so this is a soft default. |
| A26 | `app/tools/gcp_tools.py:189` | `series.points[:20]` — "Limit to 20 data points per series" | Inline comment. | **High for time-series reasoning.** 20 points over a `interval_minutes=120` window (the fallback at `gcp_metrics_tierb.py:122-123`) is one point per 6 minutes. A spike shorter than the sampling stride is invisible, and the agent is asked to classify "growing / stable / declining / spike / recovering" (`gcp_metrics_tierb.py:192`) off that. |
| A27 | `app/tools/gcp_tools.py:233` | `list(policies)[:20]` — "Limit to first 20 policies" | Inline comment. | Medium. Unordered slice: the alert policy that actually fired can be #21. |
| A28 | `app/tools/gcp_tools.py:275` | `list(services)[:20]` | Inline comment. | Medium. Same unordered-slice problem in a project with >20 Cloud Run services. |
| A29 | `app/tools/gcp_tools.py:317` | `list(revisions)[:10]` | Inline comment. | Medium. On an actively deployed service, 10 revisions can be under an hour of history — the bad revision may be off the list. |
| A30 | `app/tools/gcp_tools.py:145` | `query_time_series(interval_minutes: int = 30)`, window **always counting back from now** (`:165-171`) | Docstring. | **High** — see B.6. There is no way to query a window centered on an old alert. `gcp_metrics_tierb.py:120-125` works around this by inflating `interval_minutes` to reach back to the alert, which *also* inflates the sampling stride against the 20-point cap (A26). The two caps compound. |
| A31 | `app/tools/ado_pipeline_tools.py:22` | `_MAX_OUTPUT_CHARS = 4_000` | None stated. | Medium. |
| A32 | `app/tools/ado_pipeline_tools.py:194-195` | `msg[:300]`, `error_issues[:12]` | None stated. | Medium. 300 chars truncates a .NET stack trace to its first frame. |
| A33 | `app/tools/ado_pipeline_tools.py:201` | `failed_steps[...][:12]` | None stated. | Low. |
| A34 | `app/tools/ado_pipeline_tools.py:218` | `raw.splitlines()[-30:]` — 30-line log tail | None stated. `NEXT_STEPS.md` proposes 60-80. | **High for deploy-release.** The mechanism-level evidence for CI failures is this log tail (`ado_pipeline_tools.py:171-175`). 30 lines of an ADO log is often post-failure cleanup noise. The Task-vs-Phase sort fix (`:214`) landed; the line-count bump did not. |
| A35 | `app/tools/ado_pipeline_tools.py:54` | `get_failed_pipeline_builds(top: int = 8)` | Docstring. | Low-Medium. |
| A36 | `app/tools/ado_pipeline_tools.py:53` | `window_hours: int = 1` | Docstring. | **High.** The failure that caused the alert can precede it by more than an hour; the search window is `[alert_time - 1h, alert_time + 15min]` (`:89-90`). Contrast `deploy_correlation_tierb.py:113` which tells the model to look back **2 days** for CI events — the direct ADO tool's default silently contradicts the prompt. |
| A37 | `app/tools/octopus_tools.py:19` | `_MAX_OUTPUT_CHARS = 4_000` | None stated. | Low. |
| A38 | `app/tools/octopus_tools.py:109` | `MessageText[:160]`, `[:3]` errors per node (`:112`) | None stated. | **High for deploy-hang.** 3 error lines at 160 chars each is the entire mechanism evidence for an Octopus hang (`octopus_tools.py:86-88` calls these "the mechanism"). |
| A39 | `app/tools/octopus_tools.py:130` | `problem_steps[:10]` | None stated. | Low. |
| A40 | `app/tools/octopus_tools.py:34` | `find_deploy_tasks(take: int = 5)` | Docstring. | Low-Medium. |
| A41 | `app/tools/gcs_tools.py:13` | `_MAX_READ_CHARS = 6_000` on `read_incident_file` | None stated. | Medium. A schema-v2 incident doc (`persistence.py:121-165`) contains the full report + all 9 findings blobs — it will exceed 6,000 chars, so historical context reads a **prefix** of a past incident, not its root cause section (which sits late in the JSON). |
| A42 | `app/tools/gcs_tools.py:17` | `list_incident_files(limit: int = 20)` | Docstring. | Low. |
| A43 | `app/tools/infra_context_tools.py:22` | `_MAX_READ_CHARS = 12_000` | None stated. | Low-Medium. |
| A44 | `app/tools/infra_context_tools.py:134` | `text[:3000]` per service-catalog section | None stated. | Medium. |
| A45 | `app/tools/firestore_tools.py:83` | `find_similar_incidents(limit: int = 5)`, corrected capped at 3 (`:128`) | Docstring. | Low-Medium. |
| A46 | `app/categories.py:177` | `info['keywords'][:5]` in the classifier's category table | None stated. | Low. Categories with >5 keywords (e.g. `business-logic`, 10 keywords, `categories.py:12-23`) expose only half of them to the classifier. |

### A.4 Verification-path caps (the audit trail)

| # | Location | Value | Stated justification | Accuracy cost |
|---|---|---|---|---|
| A47 | `app/tools/session_tools.py:29` | `_MAX_EVIDENCE_CHARS = 60000` | Long comment `:19-28`: the previous 25-*item* cap dropped 4-15 diagnostic results per run on 2026-07-29. | Medium. Much better than what it replaced, but still bounded — `_build_payload` emits `evidence_partial: true` (`session_tools.py:304`) when it bites, and `verification.py:176-204` then spends 29 lines telling the verifier not to over-react to it. |
| A48 | `app/tools/session_tools.py:33` | `_RESULT_HEAD_CHARS = 900` | "Raised from 500: at 500 chars a metrics payload was cut before its data points, which is exactly the number the verifier is asked to confirm." | **High.** 900 chars is *still* the head of a payload that was already cut at 6,000 by A22 and at 4,000 by A21/A24. The verifier is asked to check "(b) The number matches" (`verification.py:220`) against a 900-char prefix of a truncated JSON blob. `verification.py:198-200` explicitly concedes the verifier will often have to say "the figure was not visible". |
| A49 | `app/tools/session_tools.py:52` | `_ARG_VALUE_CHARS = 400` | "400 is the smallest cap under which no call in those runs loses its time parameters." | Low. Empirically tuned to the one thing it must preserve. Reasonable. |
| A50 | `app/tools/session_tools.py:342` | `_truncate_dict(d, max_keys=5)` | Docstring only. | **Medium-High.** Only the first **5 arguments** of any tool call are shown. `fetch_datadog` passes a list of code strings, but a `query_gcp_logs` kwargs dict or a Bifrost `args` dict with 6+ keys silently loses keys. Check (c), the window check, depends on `from`/`to` surviving — if they are keys 6 and 7, they are replaced by `"...": "N more keys"` (`session_tools.py:347`). |
| A51 | `app/tools/session_tools.py:37-45` | `_LOW_PRIORITY_TOOLS` demotes `get_service_context`, `load_infra_context`, `listToolFiles`, `getToolDocs`, `list_incident_files`, `read_incident_file`, `get_triage_guidance` | "Tools that set up context but are never cited as evidence for a root cause." | Low. Sound prioritisation. Correctly *demotes* rather than drops (`_select_within_budget`, `:249-256`). |

### A.5 Model selection

**Every LLM agent in the pipeline runs on `create_fast_model()` — Haiku.** There are no `create_model()` (Sonnet) callers anywhere outside `config.py` itself.

| Agent | File:line | Model factory |
|---|---|---|
| `alert_classifier` | `sub_agents/classifier.py:381` | `create_fast_model` |
| `infra_context` | `sub_agents/infra_context.py:101` | `create_fast_model` |
| `historical_context` | `sub_agents/historical_context.py:81` | `create_fast_model` |
| `cloud_run_health` (tierb) | `tier1/cloud_run_health_tierb.py:112` | `create_fast_model` |
| `compute_engine_health` | `tier1/compute_engine_health.py:44` | `create_fast_model` |
| `gcp_logging` (tierb) | `tier1/gcp_logging_tierb.py:105` | `create_fast_model` |
| `gcp_metrics` (tierb) | `tier1/gcp_metrics_tierb.py:199` | `create_fast_model` |
| `datadog_observability` (tierb) | `tier1/datadog_tierb.py:568` | `create_fast_model` |
| `sentry_error` (tierb) | `tier1/sentry_tierb.py:125` | `create_fast_model` |
| `deploy_correlation` (tierb) | `tier1/deploy_correlation_tierb.py:154` | `create_fast_model` |
| `teams_context` | `tier1/teams_context.py:33` | `create_fast_model` |
| `known_issues` (tierb) | `tier1/known_issues_tierb.py:102` | `create_fast_model` |
| **`synthesis_agent`** | `sub_agents/synthesis.py:304` | `create_fast_model` |
| `tier2_dispatcher` | `sub_agents/tier2_dispatcher.py:78` | `create_fast_model` |
| all 5 tier-2 agents | `tier2/{sql_dba:102, cache:85, codebase:54, pubsub:38, auth_security:51}` | `create_fast_model` |
| **`report_agent`** | `sub_agents/report.py:320` | `create_fast_model` |
| **`verification_agent`** | `sub_agents/verification.py:316` | `create_verification_model` → **also Haiku** |
| `persistence_agent` | `sub_agents/persistence.py:217` | none (code-only `BaseAgent`) |

Details:
- `config.py:118` — `FAST_MODEL_ID` defaults to `claude-haiku-4-5`; `"same"` switches to the full model.
- `config.py:41` — `AGENT_MODEL_ID` defaults to `claude-sonnet-4-6` but **nothing reads it** except `create_model()`, which has no callers.
- `config.py:137-142` — the verification model is *documented* as Sonnet in the docstring (`:124-136`) but the code below the docstring overrides it: `verification_model = os.environ.get("VERIFICATION_MODEL_ID", fast_id)` where `fast_id` is Haiku. The comment at `:137` says so explicitly: "Haiku-only build: verification defaults to the fast model (Haiku) too." **The docstring at `config.py:124-136` is stale and contradicts the code it documents.**
- Consequence for verification independence: `config.py:132-135` argues the verifier is still independent because it "does not see the report agent's self-assessment". That argument holds for anchoring, but the verifier now shares both the model *and the model tier* with the agent it grades. **INFERENCE**: a Haiku judge grading Haiku output has correlated blind spots.
- `AGENT_MODEL_ID` / `FAST_MODEL_ID` / `VERIFICATION_MODEL_ID` appear nowhere in `agents-cli-manifest.yaml` or any deploy artifact in this repo, so the Haiku defaults are what runs. **INFERENCE** (env vars could be set at the Agent Engine level; I cannot see runtime env from source).

**Accuracy cost: highest single item in this audit.** Synthesis (the stage that commits to a root cause), report, and verification all run on the cheapest tier. Removing this constraint is (i) a pure accuracy win and (iii) has no context-overflow risk; the only risk is (ii) latency against the 600s deadline.

### A.6 Context-window blindfolds

| # | Location | Value | Justification | Assessment |
|---|---|---|---|---|
| A52 | `app/sub_agents/verification.py:331` | `include_contents="none"` | `:323-330`: the SequentialAgent injected "~103K tokens, contents=142" and Gemini 2.5 Flash returned empty. | **Mixed, and the justification is now stale.** The stated cause was a Gemini failure mode; the verifier no longer runs Gemini (A.5). The blindfold remains, and it means the verifier cannot see any agent's *reasoning* — only `{report}`, `{synthesis}`, `{classification}` via instruction injection, plus whatever survives A47-A50. It also created a second bug that had to be worked around: `session_tools.py:119-124` records that with `include_contents="none"` the model had no way to learn the session id and invented `"current"`, so **every lookup 404'd and zero citations were ever validated** until `tool_context` resolution was added. Removing it now would be a net accuracy gain but re-introduces the token-volume problem. **Recommendation: replace with a curated context injection rather than all-or-nothing.** |
| A53 | `app/agent.py:22-26` | `historical_context` moved out of the pre-Tier-1 sequence into a `ParallelAgent` beside `tier1_parallel` | `:16-21`: "This removes ~35s of historical_context latency from the sequential critical path … reclaiming time for the report/verification/persistence tail against the fixed ~600s streamQuery deadline." | **High, and under-acknowledged.** The comment asserts "`past_incidents` … is consumed by synthesis/report (never by Tier 1)". That is factually true of the current prompts, but it is *also* the reason tier-1 agents cannot use prior root causes to steer their queries. `historical_context.py:31-33` fetches `get_triage_guidance` — "learned investigation steps … Returns ordered agent list with hit rates" — and that guidance now lands **after** the agents it is meant to order have already run. See B.5. |
| A54 | `app/sub_agents/report.py:167-170` | "do NOT emit a per-agent row for each of the 16 agents … reproducing it here costs generation time on the critical path (against the ~600s deadline)" | Explicit. | Low for root-cause accuracy, **high for the Deliverable-C schema gap**: this is exactly the per-agent tool-call trace the project wants recorded, and it is suppressed. The comment claims it is "captured in the run's structured telemetry" — I find no such capture: `persistence.py:159-164` writes only `telemetry.duration_seconds`. |
| A55 | `app/sub_agents/persistence.py:1-19` | Persistence converted from LlmAgent to code-only `BaseAgent` | "~128s having Sonnet regenerate a JSON document from state that is already structured — the single largest sink on the ~600s critical path … Zero LLM latency, zero accuracy impact (it never analyzed anything — it reformatted)." | **Inert / correct.** I agree with this one: it is a pure latency win. The "zero accuracy impact" claim is accurate for what it replaced, but see C — the deterministic builder is also what makes the schema gap easy to fix. |

### A.7 Report-length caps

| # | Location | Value | Assessment |
|---|---|---|---|
| A56 | `app/sub_agents/report.py:63` | "**Whole report ≤1,800 characters.** If longer, cut the lowest-value bullets (WEAK SIGNAL first)" | **High.** 1,800 chars is ~300 words for a report that must contain: summary, status line, What Happened, Root Cause + 5-part evidence chain, Key Findings in 3 confidence bands, Historical Precedent, What Was Checked, Timeline, Recommended Actions, Links, and a telemetry table. The instruction to drop WEAK SIGNAL bullets first directly deletes the negative results that `synthesis.py:96-100` insists are findings. |
| A57 | `report.py:300` | "HARD LIMIT: the whole report ≤1,800 characters" (restated) | Same. Note the internal inconsistency: `NEXT_STEPS.md:31` proposed "target ≤4,000 chars"; the implemented value is 1,800. |
| A58 | `report.py:59` | "**≤12 words per bullet.** Count them." | Medium. Combined with "Banned words … `because`, `which`, `that`, `due to`" (`:60`), a causal mechanism statement is grammatically prohibited from expressing causation. |
| A59 | `report.py:77`, `:95`, `:136` | ≤15 words, ≤20 words, ≤15 words per bullet in specific sections | Medium. |
| A60 | tier-1 findings: `datadog_tierb.py:555`, `gcp_logging_tierb.py:101`, `gcp_metrics_tierb.py:195`, `sentry_tierb.py:118`, `cloud_run_health_tierb.py:108`, `deploy_correlation_tierb.py:135` | "Under **500 words**" on every tier-1 agent's output | **High** — this is the first link in the compression chain. See B.1. |

### A.8 Network/connection timeouts (bounded by time, not accuracy policy)

| # | Location | Value | Assessment |
|---|---|---|---|
| A61 | `tools/bifrost_mcp.py:41-42` | `BIFROST_CONNECT_TIMEOUT = 30.0`, `BIFROST_SSE_READ_TIMEOUT = 60.0` | "a 300s read timeout means a non-2xx/hung gateway stalls the whole run for 5 minutes before surfacing. Fail fast instead." Reasonable; **but** a legitimately slow Datadog aggregation over a wide window can exceed 60s and be indistinguishable from a hang. |
| A62 | `tools/bifrost_direct.py:31` | `httpx.Timeout(60.0, connect=30.0)` | Same trade-off on the live tier-1 fetch path. |
| A63 | `tools/ado_pipeline_tools.py:23`, `tools/octopus_tools.py:20` | `_TIMEOUT = 25.0` | Tight for an ADO log fetch. |
| A64 | `tools/session_tools.py:150` | `timeout=30` on the session-events API | If the session has many events this can time out → `_evidence_unavailable` → forced UNRELIABLE (`verification.py:166-173`). |
| A65 | `integrations/pagerduty/agent_client.py:34` | `_STREAM_TIMEOUT = 900` (env `IR_AGENT_STREAM_TIMEOUT`) | The client allows 900s; the server-side Agent Engine `streamQuery` deadline of ~600s is what actually binds (`agent.py:21`, `report.py:170`, `persistence.py:8`). I found **no code that enforces or measures 600s** — it is an external platform limit referenced in comments only. |

### A.9 Removal-risk summary

**(i) Removing improves accuracy, low risk:**
A1-A5 (prompt call budgets — replace with a *higher* budget, not none), A6-A8, A12, A26, A27-A29, A34, A36, A38, A46, A50, A56-A60, and **the Haiku default (A.5)**.

**(ii) Removing risks breaking the run:**
- A21 (`MAX_TOOL_RESPONSE_CHARS`) and A22 (`bifrost_direct [:6000]`) — the 1.36M-token overflow at `bifrost_mcp.py:122-123` is a documented real event. Raise substantially; do not remove. **A22 must at minimum emit a truncation marker** — currently it does not, which is a correctness bug independent of the size.
- A24 (`gcp_tools._MAX_OUTPUT_CHARS`) — same class.
- A47/A48 (evidence budget) — the prompt is what bounds; raising is safe up to the model's context.
- A61-A64 (timeouts) — raising trades run-abort risk for hang risk.
- A53 (parallel historical context) — reverting costs ~35s against the 600s deadline.
- **The 600s deadline is the binding constraint on all of the above and is not adjustable in this codebase.** Any "remove the caps" programme has to either (a) reduce the number of sequential stages, or (b) move off `streamQuery`.

**(iii) Inert:**
A9 (only one tool exists), A33, A37, A39, A42.

---

## Deliverable B — Structural reasoning defects

### B.1 The compression chain — who actually decides the root cause, and on what

**The deciding agent is `synthesis_agent`** (`sub_agents/synthesis.py:304`). `report_agent` has **no tools** (`report.py:23`, `:319-328` — no `tools=` kwarg) and is a "pure state-to-text transformation" (`report.py:325`). So the root cause is fixed at synthesis and only re-typeset afterwards.

Synthesis reads **only summaries**. Its inputs are the 9 `findings_*` state keys (`synthesis.py:48-56`), each of which is a tier-1 agent's ≤500-word *prose summary* of its own tool output. Synthesis never sees a raw tool result from tier 1.

Measured compression chain (character budgets, from source):

| Stage | Cap | Citation |
|---|---|---|
| Raw Datadog/Sentry/ADO payload | (unbounded) | — |
| → Bifrost executeToolCode result | **6,000 chars, no marker** | `bifrost_direct.py:70` |
| → (MCP path only) after_tool_callback | **4,000 chars** | `bifrost_mcp.py:34` |
| → GCP SDK tool output | **4,000 chars** | `gcp_tools.py:15` |
| → tier-1 agent finding | **≤500 words (~3,000 chars)** × 9 agents | `datadog_tierb.py:555` et al. |
| → synthesis output | no explicit cap, but `BREVITY_RULES` applies (`synthesis.py:301`) | `shared_prompts.py:185-195` |
| → report | **≤1,800 chars total** | `report.py:63` |
| → verifier's view of raw evidence | **900 chars/result, 60,000 total, 5 args/call** | `session_tools.py:29,33,342` |

So a Datadog aggregation that returns, say, 40,000 chars reaches the deciding agent as at most ~1/13th of itself, having passed through one lossy LLM summarisation, and reaches the human as part of an 1,800-char document. The verifier then tries to confirm the numbers against a 900-char prefix of the 6,000-char prefix.

**Nothing in the pipeline gives any agent access to a raw tool result other than the agent that made the call.** `get_session_tool_outputs` (`session_tools.py:94`) is wired **only** to the verification agent (`verification.py:322`) — i.e. the one agent that is explicitly forbidden from re-investigating ("Focus on evidence verification, not re-analysis", `verification.py:303`; "not investigation re-running", `verification.py:311`).

### B.2 Tier-2 dispatch is static and cannot respond to evidence

`TIER2_DISPATCH_MATRIX` (`categories.py:159-173`) is a literal dict keyed on category alone. The dispatcher prompt injects `{synthesis}` (`tier2_dispatcher.py:42-43`) — so the evidence is *present in the prompt* — and then forbids acting on it:

> "NEVER call an agent that is not mapped to this category, **even if the synthesis findings seem to suggest it**." — `tier2_dispatcher.py:61-62`

with the reason given as time budget (`:63-64`). The matrix is also pinned by test: `tests/unit/test_tier2_dispatch_matrix.py:100-104` asserts each observed run "dispatched exactly what the matrix maps".

Categories that get **zero** tier-2 investigation:
- `cloud-infrastructure` → `[]` (`categories.py:165`, comment "Tier 1 sufficient") — 6% of alerts (`categories.py:81`). This is the OOM / resource-exhaustion / health-check-failure category, and `cache_agent` exists and would be directly relevant (`tier2/cache.py:3-5` even says it is "Dispatched when category is 'cloud_infrastructure' with cache signals" — **the agent's own docstring describes a routing that the matrix does not implement**).
- `monitoring-meta` → `[]` (`categories.py:169`) — 3%.
- `unknown` → `[]` (`categories.py:172`) — 8%. **This is the worst case**: the alerts the classifier could not understand receive the *least* investigation.

Combined, ~17% of alerts by the stated frequencies get tier-1 only.

Second defect: **tier-2 findings never reach synthesis.** Pipeline order is `synthesis → tier2_dispatcher → report` (`agent.py:38-47`). Each tier-2 agent writes its own `output_key` (`findings_sql`, `findings_cache`, `findings_pubsub`, `findings_auth`, `findings_codebase` — `tier2/sql_dba.py:106`, `cache.py:89`, `pubsub.py:42`, `auth_security.py:55`, `codebase.py:58`). **None of those five keys is read by any downstream prompt** (grep confirms the only occurrences are the `output_key=` declarations themselves). What reaches the report is `tier2_findings` — the *dispatcher's* summary of the AgentTool return values (`tier2_dispatcher.py:82`), which is another LLM compression layer. And because tier 2 runs after synthesis, a tier-2 finding that **contradicts the committed root cause cannot revise it** — the report is instructed to check `tier2_findings` only for inclusion (`report.py:290-292`), never for conflict.

### B.3 There is no hypothesis generation, comparison, or refutation

The pipeline goes findings → one committed cause, and the prompts actively *prohibit* the alternatives:

- `synthesis.py:194-196`: "**Commit to exactly ONE most-likely mechanism.** Do NOT present 2, 3, or 4 co-equal candidate causes joined by 'or'".
- `synthesis.py:208-219` (rule 6): "Do NOT speculate about the DEEPER trigger… Once you have named the evidenced proximate mechanism, **STOP**. Do not append a list of unsupported underlying candidates… An unsupported sub-cause list LOWERS quality — it reads as fabrication."
- `synthesis.py:276`: "Do NOT list candidate triggers."
- `report.py:87`: "ONE cause only, never 'X or Y'."

I want to be fair to the design intent: these rules were written against a real failure mode (hedged menus of causes scoring badly), and they are *correct about the output format*. But they were applied to the **reasoning process as well as the output**, and that is the defect. There is nowhere in the pipeline where a candidate is stated, a discriminating test is named, and the test is run.

What refutation machinery exists is narrow, hard-coded, and single-hypothesis:
- `gcp_metrics_tierb.py:163-176` — the *only* explicit refutation instruction in the codebase, and it covers exactly one hypothesis (Redis/cache timeout): "If these return NO spike in the alert window, that REFUTES the Redis-timeout hypothesis."
- `synthesis.py:96-100` (item E) — negative results "rule a hypothesis OUT" and must be reported. This is refutation *reporting*, not refutation *seeking*.
- `synthesis.py:75-82` (item B) — cross-check siblings for contradiction. Closest thing to competing-hypothesis comparison, but it compares *findings*, not *hypotheses*, and its remedy is to narrow the claim, not to test which is right.

There is **no** agent, prompt section, or state key anywhere named for hypotheses, alternatives, or rejected causes. Nothing tries to disprove a candidate cause as a deliberate step.

### B.4 No retry/deepen loop; verification is terminal and cannot change the report

- **No `LoopAgent` anywhere.** Grep for `LoopAgent|max_iterations|escalate` across `app/` returns zero hits. The only orchestrators are `SequentialAgent` (`agent.py:28`) and `ParallelAgent` (`agent.py:22`, `tier1/parallel.py:13`).
- **Order is fixed**: `verification_agent` at `agent.py:45` runs *after* `report_agent` at `:44`. The report is already generated and already streamed to the caller before verification produces a score.
- **The report is not regenerated.** `report_agent.output_key="report"` (`report.py:328`) is written once. Verification writes `verification_result` (`verification.py:321`) and nothing consumes it except `persistence.py:119` and `:191-198`.
- **The client has already consumed the report before the score exists**: `integrations/pagerduty/agent_client.py:93-96` extracts the report from the `report_agent`-authored SSE events as they stream.
- `persistence.py:11-13` states this as a design property: "The user already has the report and verification before this runs, so persistence is fully off the answer path."

**Consequence:** a `WEAK` or `UNSUPPORTED` diagnosis with `automated_tier: UNRELIABLE` (`verification.py:135-147`) is recorded in Firestore and does **nothing else**. It does not re-open the investigation, does not annotate the report, does not gate delivery. The verifier is a passive scorer whose output reaches only the datastore. All of `verification.py:20-312` — 290 lines of carefully-tuned judging criteria — has zero effect on what the human reads.

### B.5 `decision_trees` is read-only seed data that never learns — confirmed

**Every read:**
1. `firestore_tools.py:153-186` `get_triage_guidance(category)` → `_get_db().collection("decision_trees").document(category).get()` (`:164-165`).
2. Called from exactly one place: `historical_context.py:88` (tool list), invoked per `historical_context.py:42` ("Call **get_triage_guidance** with the category").

**Every write:**
1. `firestore_tools.py:303-354` `seed_decision_trees()` → `doc_ref.create(seed_doc)` at `:338`, and it **skips any category that already exists** (`:334-336`). So it can only ever write the initial seed.
2. `seed_decision_trees` is exported at `tools/__init__.py:6` but is **not in any agent's tool list** — grep shows no agent references it. It is a manual/ops function.

**There is no third write path.** No code anywhere increments `incident_count`, updates `last_optimized`, bumps `version`, or modifies any `hit_rate`. Those fields are read at `firestore_tools.py:178-181` and never written after seeding.

**The seed data is essentially uniform**: of 119 `hit_rate` values in `app/data/decision_tree_seeds.json`, **110 are exactly `0.5`** (counted directly from the file). The remaining 9 (0.4, 0.6×2, 0.7, 0.8×2, 0.85, 0.9, 1.0) are hand-set. A "hit rate" that is 0.5 for 92% of steps carries no information — it cannot order anything.

**Does the guidance influence the investigation? Effectively no**, for two independent reasons:
1. **Ordering (temporal):** `agent.py:22-26` runs `historical_context` *concurrently with* `tier1_parallel`. The "priority investigation order" it returns (`historical_context.py:62-65`) is produced at the same time as, or after, the tier-1 agents it purports to order. Those agents' prompts don't reference `{past_incidents}` at all (verified: `datadog_tierb.py:411-413`, `gcp_logging_tierb.py:61-63`, `gcp_metrics_tierb.py:101-103`, `sentry_tierb.py:48-50`, `cloud_run_health_tierb.py:75-77`, `deploy_correlation_tierb.py:62-64` — each declares only `{classification}` and `{service_context}`).
2. **Structural:** tier 1 is a `ParallelAgent` (`tier1/parallel.py:13-27`) — all 9 agents always run, always concurrently. There is no ordering to influence and nothing can be skipped.

`{past_incidents}` does reach `synthesis` (`synthesis.py:45`) and `report` (`report.py:50`), so historical *incidents* inform the analysis. The *decision trees* specifically do not.

Also note the agent names in the seed file (`sentry_agent`, `gcp_logging_agent`, …) don't match the live agent names (`sentry_error`, `gcp_logging`, …, set at `sentry_tierb.py:126`, `gcp_logging_tierb.py:106`) — so even if something did consume the ordering, the names wouldn't join. The same stale naming appears in the dead `TIER1_AGENTS` list at `firestore_tools.py:39-49`.

**Dead code note (not deleting, per instruction):** `firestore_tools.py:25-37` contains a second, divergent copy of `TIER2_DISPATCH_MATRIX` that is missing the `content-archive` and `unknown` keys present in `categories.py:171-172`. Nothing imports it. `firestore_tools.py:39-49` `TIER1_AGENTS` is likewise unreferenced.

### B.6 Timestamp derivation and window propagation

**Derivation** (three tiers, deterministic-first):
1. LLM extraction into `AlertClassification.alert_timestamp` (`classifier.py:127-141`).
2. **Deterministic override** in `_backfill_alert_timestamp` (`classifier.py:329-377`), an `after_agent_callback` (`classifier.py:388`). `extract_epoch_timestamp` (`classifier.py:54-95`) pulls `from_ts`/`start_ts`/`event_ts`/`link_event_ts`/`to_ts`/`end_ts` from URLs (`_EPOCH_PARAM_RE`, `:35-38`), preferring window-start params (`:40`, `:88`), and falls back to labelled ISO fields (`_ISO_FIELD_RE`, `:46-51`).
3. The epoch param **overrides** the model, not merely backfills (`classifier.py:330-341`) — this is correct and well-tested (`tests/unit/test_alert_timestamp_anchor.py:76-86`).
4. Provenance is stamped: `alert_timestamp_source` ∈ `epoch-param-override` / `epoch-param-backfill` / `model-asserted (no epoch param to verify)` / `MISSING — queries will anchor on NOW` (`classifier.py:359-375`).

**Defect B.6a — the deterministic override probably never fires in production.** `_backfill_alert_timestamp` reads the alert text from session state:

```python
for key in ("alert_text", "alert", "user_input", "input"):
    value = state.get(key)
```
— `classifier.py:349-354`

**No code in this repository ever writes any of those four keys to session state.** The alert arrives as the `message` field of `stream_query` (`integrations/pagerduty/agent_client.py:111`; same shape in `.e2e5/run.py:35`), which ADK turns into `types.Content` and sets as `invocation_context.user_content` (`google/adk/runners.py:1341`), then appends as a `user`-authored **event** — not as session state. `CallbackContext` exposes it as `.user_content` (`google/adk/agents/readonly_context.py:38-41`), and grep confirms **`user_content` is referenced nowhere in `app/`, `integrations/`, or `tests/`.**

So at `classifier.py:355`, `alert_text` is `""`, `extract_epoch_timestamp("")` returns `""` at `:61`, and control falls to the `elif`/`else` at `:367-376` — meaning the pipeline can only ever land on `model-asserted (no epoch param to verify)` or `MISSING`. The ground-truth override that `classifier.py:330-341` describes as the fix for the 2025-08-19 wrong-year incident **cannot execute**. The unit tests all call `extract_epoch_timestamp` directly (`test_alert_timestamp_anchor.py:17`) and never exercise `_backfill_alert_timestamp`, so this is invisible to the suite.

This is the highest-severity single defect I found. **Confidence: high from source; not verified against a live run** — a live check would be to inspect a recent session's `classification.alert_timestamp_source` in Firestore and see whether any value is ever `epoch-param-override` or `epoch-param-backfill`. Fix is one line: read `callback_context.user_content` and fall back to the state keys.

**Propagation.** `alert_timestamp` is consumed via `{classification}` injection by every tier-1 agent. Window instructions per agent:

| Agent | Window rule | Citation |
|---|---|---|
| datadog | `(t−30min) .. (t+15min)`, via `from`/`to` dict-unpacked; `last 1h` if unknown | `datadog_tierb.py:416-421`, `:514` |
| gcp_logging | `(t−30min) .. (t+15min)` in `filter_str`; last 2h if unknown | `gcp_logging_tierb.py:78-83` |
| gcp_metrics | `interval_minutes` sized to reach back from *now* to the alert; 120 if unknown | `gcp_metrics_tierb.py:120-125` |
| deploy_correlation | **2 days** back for CI/DORA; `(t−30min)..(t+15min)` for everything else | `deploy_correlation_tierb.py:113-117` |
| sentry | **no window rule at all** | `sentry_tierb.py:78-106` — see B.6c |
| cloud_run_health | "deployment recency around the alert time" (prose only, no bounds) | `cloud_run_health_tierb.py:101` |
| compute_engine | none | `tier1/compute_engine_health.py:23-29` |
| known_issues | none (no time-capable tool) | `known_issues_tierb.py:80-82` |

**Defect B.6b — `query_time_series` cannot be anchored.** `gcp_tools.py:165-171` builds the interval as `[now − interval_minutes, now]`. There is no start/end parameter. For an 8-hour-old alert the agent must set `interval_minutes ≈ 540` to include it, and then the 20-point cap (`gcp_tools.py:189`) samples it at one point per 27 minutes. The prompt acknowledges the limitation (`gcp_metrics_tierb.py:122-123`: "query_time_series only supports a window counting back from now") but the tool is never fixed. **This is a 3-line change** — add `end_time`/`alert_timestamp` parameters to `query_time_series`.

**Defect B.6c — Sentry queries are entirely unanchored.** `sentry_tierb.py:78-106` contains detailed rules on org slug, project slugs, `limit=5`, and `sort="freq"`, and **not one word about a time window**. `search_issues` is called with `sort="freq"` (`:60`, `:75`), which ranks by *lifetime* frequency. This is precisely the failure mode `synthesis.py:63-73` was written to catch: "committing to 'Redis timeout (chronic Sentry issue, 224k events since 2024)' when the findings ALSO contain a handler failure logged inside the alert window". Synthesis is being asked to filter out a bias that the Sentry agent's own query design creates.

**Defect B.6d — the ADO default window contradicts the prompt.** `get_failed_pipeline_builds(window_hours=1)` (`ado_pipeline_tools.py:53`) searches `[t−1h, t+15min]` (`:89-90`), while the prompt that calls it says to look back 2 days for deploy-related evidence (`deploy_correlation_tierb.py:113`). If the model relies on the default, it silently gets a 1-hour window.

**Where the guard does work well:** `datadog_tierb.py:178-246` and `:337-403` are a genuinely strong piece of engineering — four distinct classes of ineffective time constraint are detected (`_unconstrained_time_calls`, `_ineffective_range_calls`, `_dropped_kwarg_calls`, `_sql_only_window_calls`), each with live-measured evidence in the comments (`:85-110`), and every warning path emits the literal `!! UNCONSTRAINED TIME WINDOW` marker that `synthesis.py:88-94` keys on. This is the model the other four data sources should follow. It exists for Datadog only.

---

## Deliverable C — Persistence / report schema gap

### C.1 What `schema_version: 2` captures today

From `persistence.py:_build_incident` (`:102-165`), verbatim field list:

| Field | Source | Type |
|---|---|---|
| `schema_version` | literal `2` | int (`:122`) |
| `incident_id` | `f"{YYYYmmdd-HHMMSS}-{category}"` | str (`:114`) |
| `timestamp` | `now.isoformat()` | str (`:124`) |
| `source` | literal `"ir-agent"` | str (`:125`) |
| `alert_info.raw_text` | `classification.raw_text` | str (`:127`) — **note: `raw_text` is not a field on `AlertClassification` (`classifier.py:98-147`), so this is always `None`** |
| `alert_info.source` | `classification.alert_source` | str (`:128`) |
| `alert_info.url` | hardcoded `None` | (`:129`) |
| `category`, `severity`, `service`, `environment`, `gcp_project` | classification | (`:131-135`) |
| `synthesis` | `_as_text(state["synthesis"])` — the whole markdown blob | str (`:139`) |
| `report` | `_as_text(state["report"])` — the whole markdown blob | str (`:140`) |
| `service_context` | blob | str (`:141`) |
| `past_incidents` | blob | str (`:142`) |
| `findings` | dict of the 9 tier-1 `findings_*` blobs, empty ones dropped | dict[str,str] (`:143-151`) |
| `tier2_findings` | the **dispatcher's** summary blob, not the 5 per-agent keys | str (`:152`) |
| `verification` | parsed `verification_result` JSON | dict (`:153`, parsed at `:119`) |
| `resolution` | hardcoded `{"summary": "Under investigation", "actions_taken": [], "resolved_at": None}` | (`:154`) |
| `engineer_debrief` | hardcoded `None` | (`:155`) |
| `telemetry.duration_seconds` | `now − run_start_epoch` | float (`:159-164`) |

Written to GCS (`persistence.py:186`) and Firestore `incidents/<id>` (`:187`), with verification re-written onto the doc (`:197`).

**Character of the schema:** it is a **prose archive**, not a decision record. Nine of the fields are opaque markdown blobs. There is exactly one structured, machine-queryable analytical field — `verification` — and it holds five enum scores plus three free-text notes (`verification.py:282-298`).

### C.2 The four requested things — what exists, what's missing

#### (1) Hypotheses considered and rejected, with reasons

**Exists:** nothing. **Data exists in session state at persistence time:** no.

The synthesis output format (`synthesis.py:229-300`) has no hypothesis section. Its closest fields are `### Contradictions` (`:289`) and `### Investigation Gaps` (`:292`) — and both are prose inside the single `synthesis` blob, not separable. Worse, as established in B.3, synthesis is *instructed not to enumerate alternatives* (`synthesis.py:194-196`, `:208-219`, `:276`), so the information is not merely unstored — **it is never generated.**

Required plumbing:
- **Emitting stage:** `synthesis_agent`. Needs a new mandatory output section, e.g. `### Hypotheses Considered` with per-candidate `{hypothesis, discriminating_evidence, verdict ∈ SUPPORTED|REFUTED|UNTESTED, reason, refuting_artifact}`. This requires relaxing `synthesis.py:194-196` / `:208-219` to distinguish *reasoning* (enumerate) from *output* (commit to one) — the current text conflates them.
- **Structuring:** the cleanest route is a second `output_key`. ADK `LlmAgent` supports one `output_key` and one `output_schema`. Two options: (a) give synthesis an `output_schema` (pydantic) as the classifier does (`classifier.py:386`) and keep the prose in a field of it — but note ADK forbids tools alongside `output_schema`, and synthesis has tools (`synthesis.py:310`), so this **will not work as-is**; (b) add a small code-only `BaseAgent` after synthesis that parses the section out of the blob and writes `hypotheses` to state via `EventActions(state_delta=...)`, exactly as `persistence.py:213` already does. **(b) is the workable path.**
- **Persistence:** one line in `_build_incident` — `"hypotheses": state.get("hypotheses")`.

#### (2) Playbook-chapter coverage

**Exists:** nothing. **Data at persistence time:** no. **A playbook/chapter concept does not exist anywhere in the codebase** — grep finds no `playbook`, `chapter`, or equivalent.

The nearest existing structure is `decision_tree_seeds.json`'s `triage_steps` (`{order, agent, action, tier, hit_rate}`), fetched by `get_triage_guidance` (`firestore_tools.py:153-186`). If "playbook chapter" is to mean "a triage step from the category's decision tree", then coverage = "which of the N steps did this run actually execute, and what did each return".

Required plumbing:
- The decision tree must first be **read before tier 1 runs**, which means undoing A53 (`agent.py:22-26`) or duplicating the read into `infra_context`.
- The step `agent` names must be reconciled with the live agent names (B.5 — `sentry_agent` vs `sentry_error`, etc.).
- **Emitting stage:** a new code-only `BaseAgent` (or an extension of persistence) that joins the tree's `triage_steps` against the set of agents that produced a non-empty `findings_*` key. All 9 keys are in `ctx.session.state` at persistence time (`persistence.py:174`), so a *coarse* coverage map (`step → agent ran / did not run`) **is computable today with no upstream change**. A *meaningful* one (`step → the specific action was performed`) is not, because nothing records which checklist item a tool call served.
- **Persistence:** `"playbook_coverage": [{step_order, agent, action, status, evidence_ref}]`.

#### (3) Ordered tool-call trace with outcomes

**Exists:** nothing in the schema. **Data at persistence time:** **not in session state — but retrievable.**

This is the most tractable of the four. `session_tools._pair_calls_with_results` (`session_tools.py:175-233`) already builds exactly this structure — an ordered list of `{index, agent, tool, args, result}` — by fetching the session's events from the Vertex sessions API (`:146-154`). It is currently invoked only as a tool by the verifier (`verification.py:322`) and its output is never written anywhere; `verification.py:274` asks the model only to copy the one-line `evidence_coverage` string into its JSON.

Two blockers:
- **Self-reference at write time.** `_resolve_engine_context` (`session_tools.py:55-91`) parses `APP_URL` and the session id comes from `tool_context.session.id` (`:134`). A `BaseAgent` has `ctx.session.id` (`persistence.py:174` already reads `ctx.session.state`), so the id is available. **INFERENCE**: the events API may not yet reflect the current invocation's tail at the moment persistence runs — persistence is the last agent, so all *prior* agents' events should be committed, but its own are not. That is acceptable for this purpose.
- **Truncation.** The existing renderer applies `_RESULT_HEAD_CHARS=900` (`:269`) and `_ARG_VALUE_CHARS=400` (`:349`) and `max_keys=5` (`:342`). For a *stored* trace those caps are unnecessary — GCS/Firestore have no token budget. The function would need a non-truncating mode.

Required plumbing: refactor `_pair_calls_with_results` + a new `fetch_full_trace(session_id)` helper; call it from `PersistenceAgent._run_async_impl` (`persistence.py:171`); add `"tool_trace": [...]` to the incident dict. **No prompt change and no LLM involvement required.** Also note the report currently *suppresses* the human-facing version of this (`report.py:167-170`, A54) on the grounds it is "captured in the run's structured telemetry" — it is not; building this closes that gap and makes the comment true.

Firestore caveat: a 1 MiB per-document limit applies. A full untruncated trace of ~60 calls could approach it. **Recommendation: full trace → GCS, summary + GCS pointer → Firestore.**

#### (4) Branch / routing decisions

**Exists:** nothing explicit. **Data at persistence time:** partially.

Routing decisions actually made in a run:
| Decision | Made by | Recoverable at persistence? |
|---|---|---|
| category → determines everything downstream | `alert_classifier` (`classifier.py:101-105`) | **Yes** — `classification.category` |
| tier-2 agent set | `tier2_dispatcher` per `TIER2_DISPATCH_MATRIX` (`tier2_dispatcher.py:50-57`) | **Only as prose** inside `tier2_findings`. The *intended* set is derivable in code from `TIER2_DISPATCH_MATRIX[category]`; whether the model obeyed is only in the trace (item 3). |
| compute_engine self-skip | the agent itself, prompt-driven (`tier1/compute_engine_health.py:11-12`: "return immediately: 'Not applicable'") | Only as prose in `findings_compute_engine` |
| known_issues self-skip | `known_issues_tierb.py:76-77` ("call NO tools at all") | Only as prose in `findings_known_issues` |
| teams_context hard-disabled | `tier1/teams_context.py:13` (static, always "unavailable") | Constant |
| synthesis escalation route | `synthesis.py:143-152` (route by proximate evidence) | Only as prose in `### Gap-Closure Investigation Actions` (`synthesis.py:239`) |
| service-scope re-targeting | `synthesis.py:221-227` | Only as prose |

Required plumbing:
- **Cheap, code-only, available today:** in `_build_incident`, add `"routing": {"category": category, "tier2_expected": TIER2_DISPATCH_MATRIX.get(category, []), "tier1_agents_with_findings": sorted(k for k in the 9 keys if state.get(k)), "tier1_agents_empty": [...]}`. This requires importing `TIER2_DISPATCH_MATRIX` from `..categories` into `persistence.py` and nothing else.
- **Full fidelity** (which agents were *actually called* vs. which returned "not applicable") requires the tool trace from item 3 plus a convention for self-skips — e.g. having each skip-capable agent emit a machine-readable first line rather than prose.

### C.3 Summary of C

| Requirement | Generated today? | In state at persistence? | Blocking change |
|---|---|---|---|
| Hypotheses considered/rejected | **No** — actively suppressed (`synthesis.py:194-196`, `:208-219`) | No | Prompt change at synthesis + a parser `BaseAgent` |
| Playbook-chapter coverage | No; concept doesn't exist | Partially (agent-level only) | Define chapters; re-order historical_context; reconcile agent names |
| Ordered tool-call trace | **Yes — already computed** (`session_tools.py:175-233`) but discarded | No, but fetchable | Refactor + call from `persistence.py`; add non-truncating mode; GCS for size |
| Branch/routing decisions | Partially (as prose) | Partially | ~10 lines in `_build_incident` for the coarse version |

The tool-call trace is the highest-value/lowest-effort item: the code already exists and is thrown away. The hypothesis record is the highest-value/highest-effort: it requires changing what the pipeline *thinks*, not just what it stores.

---

## Cross-cutting observations

1. **Nine of the eleven items in `TOKEN_BUDGET_RULES` and the synthesis budget cite the same two rationales** — token overflow (real, documented at `bifrost_mcp.py:122-123`) and the 600s deadline (real, external, unadjustable here). Both are genuine engineering constraints. What is not justified is applying an *overflow* remedy (call caps) where a *truncation* remedy (higher per-result caps + summarisation) would preserve more evidence.
2. **The truncation stack is uncoordinated.** Four independent caps (6,000 / 4,000 / 4,000 / 900) apply in series along one data path, each written at a different time for a different reason, and only two of them emit a truncation marker. `bifrost_direct.py:70` — the one on the primary tier-1 path — emits none, which silently violates the anti-fabrication rule at `shared_prompts.py:116`.
3. **The Datadog time-window guard (`datadog_tierb.py:178-246`) is the best code in this repo** and has no equivalent for Sentry, GCP Metrics, or ADO — the three other sources with the same class of defect.
4. **Two documented-but-unimplemented items** are worth flagging as pre-existing dead/stale code (not deleting, per instruction): the stale Sonnet docstring at `config.py:124-136`, and the duplicate/divergent `TIER2_DISPATCH_MATRIX` + unused `TIER1_AGENTS` at `firestore_tools.py:25-49`. Also unreferenced: the "tier A" agent variants (`tier1/cloud_run_health.py`, `datadog_observability.py`, `deploy_correlation.py`, `gcp_logging.py`, `gcp_metrics.py`, `known_issues.py`, `sentry_error.py`) — `tier1/parallel.py:3-11` imports only the `_tierb` versions, though `sentry_error.py` and `known_issues.py` are still imported by `tests/unit/test_false_allclear_guards.py:32-35`.

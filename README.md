# Incident Response Agent

A multi-agent LLM system that takes a raw production alert — a monitor payload, a chat
message, a PagerDuty webhook — and returns an evidence-cited root-cause triage report,
then scores its own report for hallucination and files the investigation as a reusable
incident record.

**It is a read-only diagnostician. It never remediates.**

> Portfolio copy of a system I built at work. All employer identifiers, project IDs,
> hostnames, service names, dashboard and work-item IDs have been replaced with a
> consistent fictional estate, and every artifact containing a real incident — report
> dumps, the human ground-truth corpus, the captured alert payloads — was removed rather
> than edited. The alert fixtures under `tests/eval/alerts/` are synthetic. The
> architecture, the prompts and the measurements that drove them are intact.

```
alert ─▶ classify ─▶ context ─▶ route ─▶ ┌─ 9 parallel investigators ─┐ ─▶ synthesize
                                         └─ historical context ───────┘        │
                                                                               ▼
   persist ◀─ verify ◀─ report ◀─ tier-2 specialists (conditional) ◀───────────┘
```

Built on [Google ADK](https://google.github.io/adk-docs/), deployed to Vertex AI Agent
Engine. 949 unit tests. See [`docs/ir-agent-flow.drawio`](docs/ir-agent-flow.drawio) and the
Mermaid sources in `docs/`.

## The pipeline

Each stage writes one `output_key` into ADK session state; the next stage's prompt reads it
by `{key}` injection. There is no separate blackboard.

| Stage | What it is | Notable |
| --- | --- | --- |
| `alert_classifier` | `LlmAgent` with a Pydantic `output_schema` | An `after_agent_callback` regex-extracts epoch params from monitor URLs and **overrides the model's timestamp** — the model was observed latching onto a stale example date inside a help link. |
| `infra_context` | loads a service catalog + category pattern docs from GCS | — |
| `tree_router` | **code only**, one Firestore read, median 0.39s | Computes the tier-1 plan as the *intersection* of an in-repo default with the stored decision tree: the database can remove an agent without a deploy, but can never add one the code does not list. |
| `tier1_with_history` | `ParallelAgent` | Runs historical-incident search concurrently with the fan-out purely to pull ~35s off the critical path against the ~600s stream deadline. |
| `tier1_parallel` | 9 concurrent investigators | Cloud Run, Compute Engine, GCP logging, GCP metrics, Datadog, Sentry, deploy correlation, chat context, known issues. |
| `synthesis_agent` | merges findings, ≤3 follow-up tool calls | Must commit to **one** mechanism, with a 4-link evidence chain (TRIGGER / SIGNAL / MECHANISM / CHANGE). |
| `tier2_dispatcher` | routes to 5 `AgentTool`-wrapped specialists | SQL DBA, codebase, Pub/Sub, cache, auth/security. Two categories map to no specialist. |
| `report_agent` | the only Opus stage, **no tools** | Pure state→text, hard-capped. |
| `verification_agent` | independent self-judge | Scores diagnosis and efficiency. |
| `persistence_agent` | **code only** | Writes GCS + Firestore, attaches scores, records decision-tree contributions. |

## Six decisions worth the read

**1. Every domain exists twice, and the second version is the interesting one.**
*Tier A* is a conventional ReAct agent over an MCP toolset: it must run a discovery dance
(`listToolFiles` → `getToolDocs` → `executeToolCode`), costing a round-trip per query.
*Tier B* is one `LlmAgent` with **exactly one** async tool — `fetch_datadog(queries: list[str])`
— so the model plans every query up front, calls once, and the tool fans them out
concurrently. Tier B keeps Tier A's `name` and `output_key`, so dashboards, downstream
prompts and tests do not change. Seven of nine domains run Tier B.

The docstrings record why it is not done more elegantly: calling `generate_content_async()`
from a custom `BaseAgent` raised *"ContextVar was created in a different Context"*, and
nesting a child runner inside the `ParallelAgent` got `aclose()`d mid-await and wedged the
whole phase.

**2. "Not checked" must never read as "checked, found nothing."** An unselected tier-1
agent writes an emphatic skip sentinel — *"NOT RUN — skipped by decision tree … This source
was NOT checked, so it is not evidence of absence"* — because an unset state key renders as
`""`, and an empty string reads to synthesis as a clean bill of health.

**3. Silent API failures get their own guard.** `datadog_tierb` distinguishes four ways a
time window can be quietly wrong: no window at all; `start=`/`end=` (which the server
accepts and drops); a SQL `TIMESTAMP` predicate that only intersects the rolling default
hour; and a dropped in-query `@timestamp:[X TO Y]`. It injects `!! UNCONSTRAINED TIME
WINDOW` markers that synthesis keys on to refuse the number as evidence. Sentry gets the
same treatment: it returns HTTP 400 on boolean `OR`/`AND`, which is indistinguishable from
"healthy" unless you check.

**4. The verifier never sees the transcript.** It runs with `include_contents="none"` —
feeding it the ~103K-token conversation produced empty completions. Instead it gets two
session-introspection tools: one pairing every tool call with its arguments *and* result
under a 60,000-char budget, and one doing substring search with ±300-char windows over the
*full* raw output. That second tool exists because measured tool output ran 148K–259K
characters, so the overview can only ever show 23–40% of it. Scoring is three-way —
VALID / INVALID / **UNCHECKED** — so an unsearched citation can never be counted as valid.
`HIGH` confidence is unreachable by design: it requires a human.

**5. The decision tree learns, but only from artifacts it can attribute.** An agent
*contributed* when a distinctive artifact it produced — a backticked span, a URL, an
id-shaped token — appears in the synthesis or report **and was not already handed to it**
in the alert, service context or history. Subtracting that ambient input is what makes the
number mean anything: on one agent it moves the measured rate from 0.594 to 0.120. Three
plausible alternatives are documented as rejected, with the reason each one fails. The
posterior is Bayesian with a heavy prior (0.5, weight 10.0) clamped to [0.05, 0.95], so no
step can ever be permanently dropped, and the Firestore write is idempotent by incident ID
because Agent Engine replays sessions.

A human correction outranks any number of samples (`scripts/correct_decision_tree.py`,
dry-run by default, `actor` **and** `reason` mandatory). It is deliberately a CLI and not an
agent tool: the tree is the agent's own routing config, and an agent able to rewrite it
could talk itself out of checking the source that disagrees with it.

**6. Billed cost and estimated cost are never mixed.** `_est` means list price; unsuffixed
means gateway-billed. The run total is read once at end-of-run from the gateway's log API,
settle-polling its write lag off the event loop and filtering by both invocation ID and
agent — one key bills every run and sessions are concurrent, so a time window alone merges
two live runs. When the gateway cannot answer, billed metrics are **absent, not estimated**:
the two sources measured ~15% apart on cost and ~57% apart on tokens over identical
traffic, so a mixed series sums to a total nobody can spot as wrong.

## Layout

```
app/
  agent.py                root SequentialAgent + App(plugins=[DatadogRunPlugin])
  config.py               settings; gateway URLs are REQUIRED and raise at import
  categories.py           13 alert categories + the tier-2 dispatch matrix
  tier1_routing.py        pure plan builder: default ∩ stored tree, skip-rule grammar, fail-open
  sub_agents/             classifier, tier1/, tier2/, synthesis, report, verification, persistence
  tools/                  MCP gateway + direct REST (ADO, Octopus, GCP, Firestore, GCS)
  app_utils/              serving adapters, Datadog spans, billed-cost readback
  evaluators/             two version-controlled Datadog LLM judges
integrations/pagerduty/   separate Cloud Run webhook receiver
observability/datadog/    dashboard, monitors, span metrics + an idempotent apply script
tests/unit/               949 tests across 35 modules
tests/eval/               accuracy harness, frozen rubric, synthetic alert corpus
```

## Running

```bash
uv sync --frozen
cp .env.example .env         # the two gateway URLs are required and have no default
uv run pytest tests/unit     # 949 tests, no cloud needed
python bin/e2e.py            # see Verifying
```

`app/config.py` raises at import if `BIFROST_MCP_URL` or `BIFROST_LLM_URL` is unset. That is
deliberate: they select which environment's gateway every LLM and MCP call routes through,
and the previous default sent local runs through production. The failure mode was "succeeds
against the wrong environment", which is worse than an error.

Tool access goes through an MCP gateway ([Bifrost](https://github.com/maximhq/bifrost), an
OpenAI-compatible LLM + MCP gateway) under one virtual key. Pipeline analysis uses direct
Azure DevOps REST and Octopus REST, because the gateway exposes no pipeline tools.

## Verifying

```bash
python bin/e2e.py
```

Runs what can be exercised without cloud credentials — module imports, the pipeline shape,
tier-1 routing including fail-open, the skip-sentinel wording, the decision-tree scorer and
its posterior bounds, the Datadog time-window guard, report format caps, and a sanitisation
sweep — and prints an explicit `SKIP` for each path that needs live infrastructure rather
than omitting it. Non-zero exit on any failure. `uv run pytest tests/unit` is the deeper
suite behind it.

## Deploying

```powershell
agents-cli deploy --deployment-target agent_runtime `
  --project <project> --region us-central1 `
  --service-account ir-agent-sa@<project>.iam.gserviceaccount.com `
  --cpu 4 --memory 4Gi --concurrency 9 `
  --secrets "BIFROST_API_TOKEN=<secret>:latest,..." `
  --update-env-vars "BIFROST_MCP_URL=...,BIFROST_LLM_URL=..."
```

Two flags are load-bearing, both for measured reasons:

- **`--cpu 4`** — at `cpu=1`, **4 of 5 concurrent investigations died** inside ADK's
  `ParallelAgent` TaskGroup exit.
- **`--service-account`** — omitting it falls back to a Google-managed identity with viewer
  rights on one project, the documented cause of 32 cross-project 403s and one fabricated
  metric in a live run.

The engine ID lands in `deployment_metadata.json` (gitignored; see the `.example`), which
every harness reads to find its target.

## PagerDuty integration

`integrations/pagerduty/` is an independently deployed Cloud Run service. `incident.triggered`
→ **HMAC-SHA256 verification of the signature header over the raw body is the security
boundary**, because the endpoint is public → immediate 2xx ACK → a FastAPI `BackgroundTask`
does the real work, because a run takes 200–600s. It then fetches the incident's *alerts*
(the webhook payload carries only metadata, not the diagnostic body) and posts the report
back as a note, prefixed with a confidence banner derived from the verifier's tier — four
hand-written banners **including one for missing**, because "no warning here" is not a pass
and ~22% of stored runs have no verification block.

Loop-safety: it subscribes only to `incident.triggered`, only *adds* notes, and skips
anything originating from itself.

## Observability

Datadog LLM Observability (`ddtrace`, agentless) enabled **in code** as the first statement
in `fast_api_app.py` — Agent Engine owns the entrypoint, and the OTel provider fixes its
resource at creation — running dual-stack alongside the untouched OTel → GCP pipeline. It
refuses to default the environment tag and never raises: this agent is what gets called when
production is broken.

Business dimensions go on the root span, and four are tagged **even when empty**, because a
span missing a tag drops out of a group-by entirely and reads as no-data rather than as the
unknown bucket. Per-sub-agent spans come from an ADK `App` plugin so a sub-agent added later
is covered automatically — ddtrace's ADK integration patches only the runner, tool calls and
model calls, so without it there is no per-sub-agent span at all.

Ships one dashboard, five active monitors and nine span-based metrics, applied by an
idempotent PowerShell script against the Datadog REST API. Terraform was retired for this
because the org-pinned provider has no spans-metric resource. `observability/datadog/README.md`
documents all four data stores and which tags each carries, because a query against the wrong
store returns an **empty widget, not an error**.

## Evaluation

`tests/eval/` scores root-cause accuracy against a **frozen rubric** (`accuracy-rubric.md`):
0–100 in five bands, evidence-grounded (a confident unsupported cause caps at 59), mechanism
over category, no credit for breadth. Pass bar: every alert ≥95 and mean server-side duration
≤300s. The file states that scores may not be improved by weakening the rubric.

The judge is independent and **cross-tier** — a stronger model grading the agent's reports —
reading the alert, the report and the raw findings, **blind to the agent's own verification
block** so it cannot anchor on it, and in A/B mode blind to variant identity. Reports are
mapped back to alerts by **content signature**, not timestamp order, because retries write
extra documents.

## Documentation

| Doc | What |
| --- | --- |
| `docs/ir-agent-flow.drawio`, `docs/0*.mmd` | Architecture, dedup flow, investigation workflow, data model, routing tree |
| `docs/plan.md` | Original design and its reasoning |
| `docs/research/code-audit.md` | 60+ accuracy constraints with file:line |
| `docs/google-sre-analysis.md` | What two public Google SRE papers imply for this design |
| `docs/tool-determinism-analysis.md` | Why the classifier pins `temperature=0.0` and nothing else does |
| `docs/severity-stability-measurement.md` | The measured self-disagreement rates behind that |
| `docs/ir-agent-graph-comparison.md` | Why this is a sequential pipeline and not a graph |
| `docs/ACCURACY_LOG.md`, `docs/ACCURACY_PLAN.md` | What was tried and what it scored |
| `observability/datadog/README.md` | The four data stores, their tags, and two known gaps |

## Known debt, stated rather than hidden

- **~70 ruff findings remain** (unused imports, blind `except`, implicit string
  concatenation, a few mutable class defaults). They predate publication — the original
  pipeline ran the tests but never gated on lint. `ruff check --fix` cleared 75 of them with
  the suite still green; the rest need reading, not a flag, so CI reports them without
  failing on them.
- The two architecture PNGs that rendered internal hostnames were removed rather than
  re-rendered; their Mermaid sources are in `docs/` and still render.
- `tests/eval/` scores accuracy against a *deployed* engine. Without one, the harness runs
  but has nothing to score.

## The repo's defining habit

Almost every non-obvious decision is annotated inline with the dated measurement that
produced it: the 4-of-5 lost investigations at `cpu=1`, the 32 cross-project 403s, the time
kwargs silently dropped by a vendor API, the 24-of-27 verifier searches that failed because
they were multi-token prose, the classifier's 7.4% severity self-disagreement at default
temperature, the 51% of poor tiers driven by a dimension that could only ever be MISSING.

The prompts are correspondingly long and adversarial — less instructions than accumulated
regression tests written in English, each paired with the failure it exists to prevent. The
recurring principle is **fail loud or fail open, never fail plausibly**.

## License

MIT — see [LICENSE](LICENSE) — **except** nine files generated from Google's ADK
project starter, which keep their original Apache-2.0 header. [NOTICE](NOTICE) lists them.

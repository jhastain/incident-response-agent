# IR Agent Accuracy Project — Plan

**Story:** ADO-200006 · **Branch:** `ado-200003` · **Worktree:** `<workspace>/aiops-ir-accuracy`
**Companion stories:** 284160 (async execution), 284161 (SQL feature-flag access)
**Research:** `<workspace>/aiops/agents/incident_response_agent/.research/` (4 reports, 2,124 lines)

---

## What the research changed about the plan

Four agents worked the corpus, the human RCAs, the code, and the SRE playbook independently.
Two of my going-in assumptions were falsified by evidence, and the plan is rebuilt around that.

### Falsified: "the agent fails because tier-1 doesn't gather enough"

Measured across 319 modern runs:

```
mean tier1 no-data share   WEAK runs: 43.4%    non-weak runs: 43.5%
point-biserial r(no-data share, weak outcome) = -0.005   (n=319)
```

Runs with plenty of data hedge at the same rate as runs with almost none. Runs with a
known `gcp_project` hedge *slightly more* than runs without. **Tool starvation does not
predict weak outcomes.**

### Confirmed from two independent directions: the defect is elimination and commitment

| Source | Finding |
|---|---|
| Corpus (n=319, self-reported blockers) | **74.2%** of weak outcomes state **no blocker at all** — data in hand, no wall hit, still declined to name a mechanism. Only ~14% are real tool gaps. |
| Human RCAs (n=24) | **10 documented red herrings vs 8 unreachable causes.** Every red herring is reachable by the agent today. |

The corpus says the agent won't commit; the postmortems say when it does commit it
over-commits to a reachable correlate. Same root defect: **no machinery to eliminate.**

### The case that proves it

2026-07-14/16 DB blocking. Three separate AI analyses — including an ADO bot at
**90% stated confidence** — blamed columnstore index hints. A DBA overturned all three:
the hints were present and unchanged before, during, and after both incidents, so they
cannot explain *why it happened then*. Real cause: uncapped memory grants (~125 TB/hour
requested vs ~28–120 GB used) starving the buffer pool.

The wrong answer shaped a week of follow-up work. One test kills it:

> **"Present during the incident" ≠ "changed before the incident."**

---

## Blocking defect — must land first

**The agent does not persist its own input.** `alert_info.raw_text` is null in
**319 of 480** docs (66.5%).

- **Mechanism (code-confirmed):** `persistence.py:127` reads `classif.get("raw_text")`, but
  `AlertClassification` (`classifier.py:98-140`) declares seven fields and `raw_text` is not
  one of them. Returns `None` on every run of the current pipeline.
- **Regression date:** clean cutover 2026-07-14 between 05:27 and 15:07 UTC. Zero overlap.
- **Consequence A:** 319 runs cannot be replayed, deduplicated, or scored. Unrecoverable.
- **Consequence B:** `persistence.py:116` derives the GCS filename from the same null, so it
  degenerates to `<date>_sev<n>_<category>_<service>_<category>.json`. **196 of 319 runs
  (61.4%) overwrite an earlier file.** This is happening in production now — a read failed
  mid-analysis with `404 generation=...: No such object` because the object was replaced
  between listing and reading.
- **No recovery path:** GCS is written from the same dict; modern GCS files carry the
  identical `"raw_text": null`.

Fix: source the alert from `ctx.user_content` (verified present on ADK `InvocationContext`
in the installed SDK), not from the classifier's echo. Add `incident_id` to the GCS
filename so collisions are structurally impossible.

**Nothing else in this project is measurable until this lands.**

---

## Accuracy target (agreed)

**80% of incidents whose decisive evidence the agent can actually reach**, with the ceiling
reported separately. Measured reachability against human RCAs (n=24):

| Reachability | n | % |
|---|---:|---:|
| YES — reachable today | 9 | 37.5% |
| PARTIAL — strong lead, cannot close unaided | 7 | 29.2% |
| NO — evidence in a source the agent cannot touch | 8 | 33.3% |

An 80% *absolute* mechanism-level bar is not achievable with current tool access; SQL Server
internals alone block 5 of 24. Unreachable incidents are scored as
**"correctly identified as needing X access"**, not as failures — and the capability gaps
become their own tracked backlog.

Second hard metric, per your instruction: **no false confidence.** If a claim is not
verifiable live, it is marked as such and used to inform, never asserted as fact.

---

## Phases

Each phase runs under the `sre:agentic-loop` skill: smallest viable change → local run →
measure against criteria → iterate until met → next area. One area at a time.

### Phase 0 — Restore the input (blocking)
Add `raw_text` to the persisted record from `ctx.user_content`; add `incident_id` to the GCS
filename. **Exit:** a local run writes a doc with non-null `raw_text` and a unique filename;
replay of the same alert produces a second distinct file.

### Phase 1 — Benchmark + v2 rubric
- **Gold tier:** 24 human-RCA records (17 HIGH, 7 MEDIUM), each tagged reachable/partial/unreachable.
- **Silver tier:** ~30 stratified from the GCS corpus, cause established by live research.
- v2 rubric supersedes `tests/eval/accuracy-rubric.md` (which demands ≥95 on every alert AND
  ≤300s — contradicts both the 80% bar and accuracy-over-speed). Old file marked superseded;
  scores across the two are not comparable.
- Offline judge on Opus, fully separate from the in-pipeline verifier.

**Exit:** benchmark committed with provenance per label; judge reproduces human labels on the
17 HIGH records.

### Phase 2 — Baseline
Run unmodified agent against the benchmark. This is the number 80% is measured against.
Also measure consistency — impossible today (Phase 0 unblocks it). Note: the *old* pipeline
was consistent (0/29 divergent on Set A); the current pipeline has never been measured.

### Phase 3 — Elimination discipline (the main event)
Highest measured leverage. Four changes:

1. **Differential test as a hard gate.** Every candidate cause must be shown to have
   *changed before* the incident, not merely been *present during* it. Enforced in synthesis
   and re-checked by the verifier. This single test kills all three wrong DB-blocking analyses.
2. **Explicit refutation pass.** Generate competing hypotheses, attempt to disprove each
   against gathered evidence, then commit. Requires relaxing `synthesis.py:194-196` and
   `:208-219`, which currently ban enumerating alternatives — the ban was written against
   hedged *output* but was applied to the *reasoning*, so the hypothesis record you want is
   not merely unstored, **it is never generated**.
3. **Ban unfalsifiable confidence.** No confident mechanism claim where the decisive evidence
   sits in an unreachable source. Name the gap and the exact query a human should run.
   Not-100%-verifiable findings are marked as such and inform but never assert.
4. **Red-herring library.** Encode the ten documented time-wasters (deploy proximity, Valkey
   CPU spike, co-occurring SQL transaction spikes, single-customer runaway jobs, security-attack
   look-alikes) as explicit rule-outs.

**Exit:** on the DB-blocking benchmark case the agent either names memory grants or explicitly
reports "cannot determine — needs Extended Events on memory grants"; it must **not** blame the
columnstore hints.

### Phase 4 — Lift the caps
Now, not before — extra depth is only worth buying once elimination works. Per the audit's
removal-risk table:

- **Safe to lift:** prompt call budgets (raise, don't remove), 1,500-word output cap, 3-call
  synthesis budget, dead-end-after-3 rule, `include_contents="none"` blindfolds, evidence
  budget (60,000 / 900 / 400 chars).
- **Raise, do not remove:** `MAX_TOOL_RESPONSE_CHARS`, `bifrost_direct [:6000]`,
  `gcp_tools._MAX_OUTPUT_CHARS` — a 1.36M-token overflow is documented at
  `bifrost_mcp.py:122-123`. **`bifrost_direct.py:70` emits no truncation marker at all**,
  which silently violates the anti-fabrication rule; that is a correctness bug independent of size.
- **Models:** synthesis / report / verification → Opus 5; Tier-1 fetchers stay Haiku. Verified
  clean: no `temperature`/`top_p`/`budget_tokens` anywhere in `app/`, so no 400 risk. Opus 5
  thinks by default and `max_tokens` caps thinking + text together — set explicit headroom on
  the report agent.
- **Not adjustable here:** the ~600s `streamQuery` deadline is a platform limit (story 284160).

**Exit:** accuracy up vs Phase 2 baseline, runs still complete inside 600s deployed.

### Phase 5 — Decision-tree persistence
All four components you asked for. Effort ranked by the audit:

| Component | State today | Work |
|---|---|---|
| Ordered tool-call trace | **Already computed and thrown away** — `session_tools.py:175-233` builds it, only the verifier sees it | Refactor + non-truncating mode; full trace → GCS, pointer → Firestore (1 MiB doc limit) |
| Branch/routing decisions | Partially derivable | ~10 lines in `_build_incident` |
| Playbook coverage matrix | Concept does not exist in the codebase | Define chapters; reconcile agent names |
| Hypotheses considered/rejected | **Never generated** (see Phase 3) | Depends on Phase 3; parser `BaseAgent` writes state |

Note `report.py:167-170` justifies suppressing the telemetry table because it is "captured in
the run's structured telemetry" — it is not. This closes that gap and makes the comment true.

### Phase 6 — Playbook coverage + learning loop
- Add reachable missing Tier-1 checks: GCP status dashboard, CI/CD config-change revisions,
  single-host-vs-fleet outlier test, cache instance existence, Pub/Sub subscription config.
  Feature flags via Teams as interim (story 284161 for SQL).
- Rewrite `decision_tree_seeds.json` from the playbook; every step states what it **rules out**.
- Close the learning loop — `decision_trees` is currently write-once seed data with `hit_rate: 0.5`
  on every step and nothing ever writes back.
- Fix `pubsub_agent`: its only tool is `query_gcp_logs`, so the Pub/Sub specialist **cannot read
  a metric**.

---

## Deferred, with reasons

| Item | Why | Tracked |
|---|---|---|
| Async/resumable execution | Removes the 600s ceiling properly; large change to `agent.py` + both callers | 284160 |
| SQL Server access | Blocks 5 of 24 human RCAs — the single largest capability gap | 284161 + awaiting your guidance |
| Verifier can re-open an investigation | `verification.py` runs *after* the report is streamed (`agent_client.py:93-96`); 290 lines of judging criteria currently change nothing. Fixing needs pipeline reordering | Propose after Phase 3 |
| Tier-2 dispatch driven by evidence | Static category map; `cloud-infrastructure`/`monitoring-meta`/`unknown` (~17%) get zero Tier 2. Tier-2 findings also never reach synthesis | Propose after Phase 3 |

---

## Operating agreement

- **Autonomous** on anything read-only or local. Full permissions in the worktree.
- **Ask first** for anything writing to a shared system (deploy, prod, Teams, PR merge).
- **Hourly written status** to `docs/ACCURACY_LOG.md` plus an in-session summary.
- Every claim carries a file:line or a live query. Hypotheses are labelled as hypotheses.
- Dev Firestore is receiving live writes from another process (observed 21:27 today) — the
  benchmark uses a separate collection so we never contaminate or depend on it.

# IR Agent Accuracy — Running Log

Story ADO-200006 · branch `ado-200003` · worktree `<workspace>/aiops-ir-accuracy`

---

## 2026-07-30

### Phase 0 — Research synthesis · COMPLETE

Four agents ran read-only against the corpus, human RCAs, agent code, and the SRE playbook.
Reports in `docs/research/` (2,124 lines).

**Two going-in assumptions falsified.**

1. *"Accuracy is lost because tier-1 under-gathers."* False. Across 319 modern runs,
   mean tier-1 no-data share is 43.4% on weak outcomes vs 43.5% on non-weak;
   point-biserial r = −0.005. Runs with a known `gcp_project` hedge *slightly more*
   than runs without.

2. *"Repeat runs of the same alert diverge."* Withdrawn — it was an artifact of clustering
   on report keywords rather than alert identity. On the only subset where alert identity is
   knowable (n=161), the agent was consistent: 0/29 divergent, 13/29 at 100%. The v1 report
   was voided by its own author after the methodology was challenged.

**What the evidence actually says.** Two independent sources converge:

| Source | Finding |
|---|---|
| Corpus, self-reported blockers (n=319) | **74.2%** of weak outcomes state no blocker at all — data in hand, no wall hit, no mechanism named. ~14% are real tool gaps. |
| Human postmortems (n=24) | **10 documented red herrings vs 8 unreachable causes.** Every red herring is reachable by the agent today. |

The defect is elimination and commitment, not access. Canonical case: the 2026-07-14/16 DB
blocking pair, where three AI analyses (one at 90% stated confidence) blamed columnstore
hints that were unchanged before, during, and after — overturned by a DBA applying one test:
*"present during" ≠ "changed before."*

**Reachability ceiling** (decisive evidence, n=24): 37.5% YES · 29.2% PARTIAL · 33.3% NO.
An absolute 80% mechanism-level bar is not achievable with current tool access. Target agreed
as 80% of *reachable* incidents, ceiling reported separately.

### Phase 0b — Restore the alert input · IN PROGRESS

**Defect.** `alert_info.raw_text` null in 319/480 docs (66.5%), clean regression from
2026-07-14 (last with text 05:27:31 UTC, first without 15:07:09 UTC, zero overlap).
`persistence.py` read `classif.get("raw_text")`, but `AlertClassification` declares seven
fields and `raw_text` is not one — so the lookup returned `None` on every run.

Second-order: the GCS filename derived from the same null and collapsed to
`<date>_sev<n>_<category>_<service>_<category>.json`. **196 of 319 runs (61.4%) overwrote an
earlier object.** Confirmed live — a read 404'd mid-analysis because the object was replaced
between listing and reading.

**Change** (`app/sub_agents/persistence.py`):
- `_content_text()` helper joins the text parts of a genai `Content`.
- `PersistenceAgent._run_async_impl` seeds `alert_raw_text` from `ctx.user_content` —
  ADK-native, verified present on `InvocationContext` in the installed SDK. Cannot be dropped
  by a prompt or schema change, which is how the original route broke.
- `_build_incident` reads that, falling back to the classifier echo.
- Filename now carries `incident_id`, making collisions structurally impossible.

**Verification:**
- `tests/unit/test_alert_input_persisted.py` — 5 tests. Confirmed **failing 4/5 before** the
  fix (including the collision case), passing 5/5 after.
- Full unit suite: **178 passed**, no regressions.
- Ruff: clean on both changed files. Baseline check via `git show HEAD:` confirms the 5
  pre-existing errors in `persistence.py` were already there — none introduced.
- End-to-end local run against live tools: **in progress**.

**Live result:** two runs of a byte-identical alert produced non-null `raw_text` and two
distinct GCS objects:

```
BEFORE  2026-07-30_sev2_application-errors_webcore_application-errors.json      <- every run collides
AFTER   2026-07-31_..._datadog-monitor-WebCore-line_20260731-050214-application-errors.json
        2026-07-31_..._datadog-monitor-WebCore-line_20260731-050835-application-errors.json
```

Committed as `1c6314d`.

### Phase 0c — Local runs could not verify · DONE

Found by the Phase 0b end-to-end run. `get_session_tool_outputs` resolved the session via
`APP_URL` and fetched events from the Vertex `reasoningEngines` sessions API — neither
exists under `InMemoryRunner`. Every local run returned `evidence_unavailable`, so the
verifier auto-tiered UNRELIABLE with 0 valid citations regardless of report quality
(observed: 381.9s, 31 tool calls, complete report, `evidence_citations_invalid: 47`).

Local runs are where this project iterates, so this would have silently corrupted every
accuracy number in Phase 2.

Fix reads the in-process session when `APP_URL` is absent, normalising events to the REST
shape so one parser serves both paths. Deployed path stays authoritative. A session with
events but no tool calls still reports `evidence_unavailable` rather than rendering an
empty payload the verifier would read as "checked, found clean".

**Live result (run 3):** the verifier now performs real per-citation validation — checks a
claim against the actual `fetch_datadog` call, catches a CST/CDT inconsistency in the
report, names the specific query that errored. Compare run 1, which could only say
"evidence unavailable".

### Phase 0d — The timestamp override never fired · DONE

Run 3 surfaced a worse defect. The alert carried `from_ts=1785443700000`
(= 2026-07-30T20:35:00Z); the pipeline stored **2026-05-30T17:35:00Z** — two months and
three hours off.

`extract_epoch_timestamp` reads the alert correctly in isolation (verified directly). The
fault was in `_backfill_alert_timestamp`, which looked for the alert in state keys
`alert_text` / `alert` / `user_input` / `input`. **Nothing in the runner path sets any of
them**, so `alert_text` was always `""` and the override this callback exists to perform
never fired on any run. A guard that was structurally dead while appearing present — the
same failure shape as the dropped `raw_text`.

This is the highest-damage single field in the pipeline: every downstream log and metric
query anchors on it, so a wrong value yields a report that is internally consistent about
the wrong day. The existing `test_alert_timestamp_anchor.py` warns about exactly this
("a confidently WRONG date is worse than a missing one") and passed throughout, because it
tests the extractor in isolation rather than the wiring.

Fix falls back to `callback_context.user_content`. Pre-seeded state keys still win.

**Verification:** 7 new tests; **4 confirmed failing against HEAD**, all 7 pass after. The
3 that pass both ways are the preserved guards (no-epoch, nothing-at-all, absent content).
Full suite 189 passed. Ruff: my files clean; the 3 UP017 errors in `classifier.py` are
pre-existing at HEAD (verified via `git show`) and left alone.

**Live confirmation, and a measurement error worth recording.** Run 4 appeared to show the
fix failing — stored timestamp `2026-05-30T14:28:20Z`, `alert_timestamp_source: None`. That
reading was wrong. `local_run.py` captured `classification_text` from the classifier's
**streamed event**, which is emitted *before* the after-agent callback runs, so it can never
show the override regardless of whether the fix works.

Probing real session state directly (`.p0/probe.py`, classifier only):

```
alert_timestamp        : 2026-07-30T20:35:00Z          <- correct
alert_timestamp_source : epoch-param-override (model said 2026-06-01T14:15:00Z;
                         embedded epoch param says 2026-07-30T20:35:00Z — the epoch wins)
model_value            : 2026-06-01T14:15:00Z
```

The fix works and the audit trail is intact. `local_run.py` now also emits the
state-resolved `classification` alongside the streamed text, so the harness cannot report a
pre-callback value as final again. This mattered beyond one fix: every future accuracy
measurement reads through this harness.

### Phase 0e — Harness reported pre-callback state as final · DONE

Covered above. `tests/eval/local_run.py` returns `classification` from session state in
addition to `classification_text` from the stream.

### Open items

- **Bifrost token absent in local runs** — Datadog/Sentry/ADO calls fail with "no Bifrost
  token". Local tool coverage is therefore partial, which caps what a local baseline can
  measure. Needs resolving before Phase 2.
- `gcloud` CLI auth expired mid-session (ADC still works).

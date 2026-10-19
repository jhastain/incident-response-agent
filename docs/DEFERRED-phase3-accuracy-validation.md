# DEFERRED — Validate Phase 3 accuracy changes (do AFTER the agent moves to test/prod)

**Status:** blocked, intentionally. Do not start until the IR agent is running in **test/prod**
rather than dev.

**Owner:** unassigned · **Story:** ADO-200006 · **Draft PR:** #109254

---

## Why this is deferred

The Phase 3 change is a behavioural change to the agent's reasoning, and validating it needs
statistical power that the dev environment cannot give cheaply:

- Agent run-to-run standard deviation is **18.6 points** (measured, not assumed).
- The headline metric is a mean over **n=7** records, so its 95% interval is roughly **±14
  points**.
- A single-run before/after therefore cannot distinguish a real 8-point gain from noise. The
  minimum honest design is **k=3 runs per record**: 15 records × 3 ≈ **8 hours** of agent time.

Running that against dev is possible but wasteful, and dev's tool surface and quotas differ
from what production actually sees. Once the agent is in test/prod the same harness produces a
number that reflects the environment the accuracy bar is actually about.

There is also a hard dependency: **the ~600s `streamQuery` deadline (ADO-200007) only applies
deployed.** Local runs have no ceiling, and at baseline the mean run was already **750s** with
a max of **6,250s**. Phase 3 adds ~33k characters of prompt. It is entirely possible for this
change to raise local accuracy while making deployed behaviour worse by pushing more runs past
the cutoff. That trade-off is unmeasurable until it runs where the deadline exists.

---

## ✨ What needs doing

### 1. Sync the environment

`main` added `ddtrace==4.12.0` (PR 109160, Datadog Agent Observability) and
`app/evaluators/*.py` imports it at module scope, so the agent will not load without it.

```powershell
cd <repo>\agents\incident_response_agent
uv sync
.venv\Scripts\python.exe -m pytest tests/unit -q     # expect 260 passing
```

### 2. Fast qualitative check first (~30 min, do this before the 8-hour run)

Do not spend 8 hours before confirming the prompts land at all. Run 2–3 benchmark records on
the Phase 3 branch and read the reports for the behaviours the change targets:

- Does a `### Ruled Out` section appear, populated with refuted candidates?
- Does the committed cause state **when the factor started**, not just that it was present?
- On an expired-telemetry record, does it now say the data is gone instead of inventing a
  mechanism?

The sharpest single probe is **`2026-06-09-auth0-credential-stuffing-ddos`** — its telemetry
has aged out of GCP's 30-day log window, and at baseline the agent fabricated a mechanism from
empty results on all three runs (27 / 34 / 72). If Phase 3 works, that record should now report
the gap rather than a cause.

```powershell
.venv\Scripts\python.exe tests\eval\baseline_run.py `
  --only 2026-06-09-auth0-credential-stuffing-ddos `
  --out tests\eval\results\phase3-smoke
```

If the reports look indistinguishable from baseline, **stop** — the prompts are not landing and
the full run would be wasted.

### 3. Full k=3 measurement

```powershell
.venv\Scripts\python.exe tests\eval\baseline_run.py `
  --repeat 3 --concurrency 3 --out tests\eval\results\phase3-k3
```

Compare against the committed baseline in `tests/eval/results/baseline/run1/baseline.json`.

### 4. Report all three of these, not just the mean

| Metric | Baseline | Why it matters |
|---|---:|---|
| gold REACHABLE mean | **45.0** (n=7, bar ≥80) | the headline |
| differential test failures | **14/18 (78%)** | what Phase 3 primarily targets |
| overclaimed beyond evidence | **12/18 (67%)** | rubric release blocker, counted separately |
| fabricated from empty data | **3/3** decision-tree records | the clearest single defect |
| run-to-run SD | **18.6** | see below |
| mean wall time | **750s** (max 6,250s) | must not get worse against the 600s deadline |

**Variance may matter more than the mean.** At baseline the widest-spread records were the
thin-evidence ones (spreads of 45 and 48 points) and the tightest was the one with the richest
telemetry (spread 10) — exactly what the elimination defect predicts. An SRE paged at 2am gets
**one** run, so a change that narrows the spread is valuable even if the mean moves little.

---

## 🎯 Acceptance

- [ ] `uv sync` done; suite green
- [ ] Qualitative check shows the targeted behaviours actually appear in reports
- [ ] k=3 run complete against test/prod
- [ ] Headline movement reported **with** its confidence interval — a change under ~8 points at
      k=3 is not distinguishable from noise and must not be claimed as an improvement
- [ ] Deployed wall time verified against the ~600s `streamQuery` deadline
- [ ] PR #109254 description updated with real before/after numbers, then taken out of draft

---

## 🧪 Context a reviewer will need

**PR #109254** (draft) changes two prompt sources:

- `app/shared_prompts.py` — four new reasoning modules (`DIFFERENTIAL_TEST`,
  `ELIMINATION_DISCIPLINE`, `RED_HERRING_CLASSES`, `EVIDENCE_ABSENCE_RULE`), placed there so
  they reach all 8 Tier 1 agents **and** synthesis rather than one path.
- `app/sub_agents/synthesis.py` — the load-bearing fix. Rule 1 banned enumerating
  alternatives; it was written against hedged *output* but applied to the *reasoning*, which is
  why the hypothesis record was never generated at all. It now bans the hedged output and
  **requires** the internal elimination, with a `### Ruled Out` section.

**Generalisation constraint (explicit user directive).** These rules must improve reasoning in
general, never patch a specific benchmark alert. With only 15 replayable records the benchmark
is small enough to game by naming its services in the prompt, which would raise the score
without improving the agent. A parametrised test asserts none of the four modules contains any
of 30 benchmark-specific identifiers. **Keep that constraint when acting on results** — if the
k=3 run shows a specific record scoring badly, the fix is a better general rule, not a rule
about that incident.

**Stacking.** #109254 targets `ado-200003-measure` (PR #109253, measurement infrastructure,
zero `app/` changes). #109253 merges first; #109254 then retargets to `main` automatically.

Full detail: `docs/ACCURACY_LOG.md` in the `ado-200003-measure` branch.

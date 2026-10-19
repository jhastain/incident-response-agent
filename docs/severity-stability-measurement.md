# Severity stability — post-deploy measurement protocol (ADO-200012)

AC #1 asks for k=3 stability on identical input. **That cannot be measured from the repo**
— it requires running the deployed agent. This document is the exact procedure to run
once ADO-200012 is deployed to the test engine, and the criteria that decide whether the
fix worked.

Nothing here was executed as part of the ADO-200012 code change. The code change is
`temperature=0.0` on the classifier plus prompt edits; whether the temperature actually
reaches the model through Bifrost is **unverified** and is what step 3 tests.

---

## Why k=3 is not enough

The measured per-draw flip rate before the fix is **7.4%** (7 of 95 docs across 22 groups
of byte-identical alert text). A majority vote over k draws still lands on the minority
answer at:

| k | P(majority vote picks the minority answer) |
|---|---|
| 1 | 7.4% |
| 3 | 1.55% |
| **5** | **0.36%** |

More important than the error rate: **k=3 cannot distinguish 75/25 from 80/20**, and
those are exactly the two cases that need opposite treatment.

- `cloud-infrastructure-startup-probe` — sev2 ×3, sev3 ×1 (n=4) against **sev3** ground
  truth. The mode is wrong → real miscalibration → fixed by the prompt change.
- `application-errors-apigateway-mobile` — sev2 ×4, sev3 ×1 (n=5) against **sev2** ground
  truth. Correct 80% of the time. The n=3 benchmark drew its tail. **Do not tune against
  this scenario** — that is fitting to noise.

**Use k=5 minimum for accept/reject. k=3 is a smoke check only.**

---

## Protocol

### 1. Baseline BEFORE deploying the change

Run all three ground-truth scenarios ×5 against the currently deployed engine. Record
the **full distribution** per scenario for **all four** classified fields
(`category`, `severity`, `service`, `environment`) — not the modal value, and not just
the first answer. A single-run benchmark is what produced the misleading n=3 result;
do not repeat it.

Ground truth lives in `tests/eval/ground-truth/*.json`:

| scenario_id | category | severity | service | environment |
|---|---|---|---|---|
| `cloud-infrastructure-startup-probe` | cloud-infrastructure | sev3 | cloud-run | test |
| `sql-database-preferences-blocking` | sql-database | sev2 | WebCore | production |
| `application-errors-apigateway-mobile` | application-errors | sev2 | WebCore | test |

### 2. Deploy the change to the **test** engine

Dev/test first. Never prod without explicit approval.

### 3. Confirm the temperature actually reached the model

This is the step that can invalidate everything downstream. ADK-side forwarding is
confirmed against the installed `google-adk` 2.5.0 (`lite_llm._get_completion_inputs`
allow-lists `temperature`; `flows.llm_flows.basic._build_basic_request` copies the agent
config onto the request — both pinned by unit tests in
`tests/unit/test_severity_calibration.py`). **The Bifrost gateway side is not confirmed.**

Two independent checks:

- Inspect a Bifrost request log for the classifier call and confirm `temperature: 0` is
  present in the payload forwarded upstream to Vertex/Anthropic.
- Behavioural: re-run one scenario ×5 (step 4). **A severity flip at t=0 means the
  parameter is not reaching the model.** Fix the forwarding before drawing any conclusion
  about the prompt — a prompt change measured at temperature 1.0 measures nothing.

### 4. Re-run ×5 and score all four fields

Success criteria, in order:

1. **Stability (the AC):** 5/5 identical `severity` per scenario. Also record category,
   service, environment stability — the same sampler drives every field, so all four
   should tighten together.
2. **Accuracy (AC #4):** category 3/3 and service 3/3 must not regress. Category matters
   more than severity here — see the blast-radius note below.
3. **Calibration:** read the modes. If `startup-probe` now stabilises on **sev3**, the
   prompt fix worked. If it is a stable **sev2**, the miscalibration survived the rewrite
   and the blast-radius rule needs another pass.

### 5. Replay the Firestore duplicate corpus

The 22 duplicate groups already in Firestore (`aiops-test-project`, collection
`incidents`) are a **free regression corpus** — larger and more realistic than the
3-scenario benchmark. Replay them post-fix and confirm the flip rate drops from 7.4%.

Grouping method (read-only): SHA-1 the whitespace-normalised `alert_info.raw_text`, group
by that hash, keep groups with 2+ docs, count distinct values per field. Note the schema:
fields are **flat** on the doc (`severity`, `category`, `service`, `environment`), not
nested under a `classification` key — a `classification` lookup returns `None` on all 124.
Query without `order_by` so no composite index is needed.

---

## What to watch for

**Category regression is the real risk, not severity.** Severity has almost no blast
radius: it sets one emoji in the Teams/PagerDuty note, one stored Firestore field, and
part of a GCS filename. It is never read by `integrations/pagerduty/` — the only field
consumed from the agent result is `report` — so a wrong severity cannot page anyone, set
PD urgency, or suppress a real incident.

Category does have a second-order blast radius: `TIER2_DISPATCH_MATRIX` routes on it, and
`cloud-infrastructure` maps to an empty agent list. A drift from `cloud-infrastructure` to
a dispatching category changes which Tier-2 agents run and what the investigation costs.
Score category on every run.

**A frozen wrong answer is the one new risk from pinning.** A scenario that was previously
correct *by luck* — landing on the right answer on a minority draw — will freeze on its
modal (wrong) answer at t=0. This is why step 1 baselines the full distribution of all
four fields before the change, rather than a single run.

---

## Stated unknowns

- Whether Bifrost forwards `temperature` unmodified to Vertex/Anthropic. Unverified;
  step 3 is the test.
- Whether t=0 eliminates flips entirely. Anthropic does not guarantee bit-identical
  output at temperature 0, and MoE routing/batching can still vary. Expect a large
  reduction, not necessarily zero.
- Whether all 124 corpus docs came from the same engine and model version. Not verified
  per doc, and the corpus spans multiple dates — some apparent instability could be
  version drift rather than sampling. The three benchmark scenarios are all from
  2026-08-07 within ~95 minutes, so those groups are clean; the corpus-wide 7.4% carries
  this caveat.
- sev1 and sev4 boundaries are essentially untested in production (3 of 124 runs
  combined). The worked examples added for those levels are reasoned from the general
  heuristic, not calibrated against live data — any claim about sev1 calibration is
  unsupported by evidence in either direction.

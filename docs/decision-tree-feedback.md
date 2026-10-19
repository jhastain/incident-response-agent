# Decision-tree feedback loop (ADO-200024)

How the per-category `decision_trees` documents learn, how an SRE corrects them
by hand, and why the two paths are separate.

Before this story `seed_decision_trees()` was the only writer and it skips any
document that already exists, so the 12 live trees in `aiops-dev-project` had
sat at `hit_rate: 0.5` / `incident_count: 0` / `last_optimized: null` since they
were seeded. Verified 2026-08-20; only `monitoring-meta` differed, hand-edited to
v2 on 2026-07-30 after the Hangfire red-herring postmortem.

---

## 1. What "contributed" means

> An agent CONTRIBUTED to an incident when at least one distinctive artifact it
> produced was reused downstream -- it appears in the synthesis or the final
> report -- and that artifact was not already handed to the agent in its inputs.

Implemented in `app/tools/decision_tree_learning.py::score_contributions`. The
full argument, including the measurements behind each clause, is in that module's
docstring. In brief:

| Clause | Why |
|---|---|
| distinctive artifact | Backticked spans, URLs, id-shaped tokens. Prose is excluded: two agents describing the same outage in English is not evidence of reuse, a verbatim revision id is. |
| reused downstream | AC 1 names both the report and synthesis. Synthesis is where cross-validation happens; evidence can be decisive there and still be dropped from the report's STRONG-only Key Findings. |
| not already handed | Tokens present in the alert, `service_context`, or `past_incidents` are subtracted. Without this every agent scores ~0.9 by restating the service name it was told to investigate. |
| produced | Tool and function names are dropped -- `search_issues` is the agent narrating its plumbing. |

### Rules that were rejected, with the measurement

**"The findings key is non-empty."** Measured over 483 incident documents
carrying a `findings` map: all nine `findings_*` keys are present and non-blank
in every one. 0 of 4347 fields are empty; 3908 exceed 2000 characters. The rule
scores 1.00 for every agent -- not a weak signal, a constant. It would write
`hit_rate: 1.0` everywhere, which is strictly worse than today's uniform 0.5
because it looks learned.

**The agent's own `- **Confidence**:` bullet.** Compliance with
`shared_prompts.OUTPUT_FORMAT_TEMPLATE` has collapsed: of 483
`findings_cloud_run` fields only 26 carry a parseable bullet, 454 never use the
word. By month, 27 of 368 in 2026-07 and 1 of 115 in 2026-08. A parser on it
measures prompt drift, not investigation quality. It is also self-assessment,
which is the anchoring bias `verification.py` exists to avoid.

**The verification score, as the per-agent signal.** It is the best independent
quality signal the pipeline has, but it is scoped to the whole investigation and
cannot say which of nine agents earned a STRONG diagnosis. It is used as a
**gate** instead: runs the verifier called `UNSUPPORTED` or `UNRELIABLE` are
excluded entirely, because tracing whose evidence reached the conclusion is
measuring the path to a wrong answer. Distribution over the 483: MEDIUM 171,
UNRELIABLE 173, LOW 54, unparseable 76, no tier 9.

### Judgment calls

1. **One artifact is enough.** An agent whose whole contribution was the single
   Sentry id that turned out to be the answer contributed. At a two-artifact
   threshold the distribution halves and three agents fall under 0.06, which
   reads as "these agents never help" -- an artifact of the threshold.
2. **`AMBIENT_MAX = 3`.** A token emitted by four or more of the nine agents is
   shared context; crediting all four is attribution by coincidence. Two or three
   converging is corroboration, which `INVESTIGATION_RULES` #2 requires. This is
   the softest number here; it moves rates by 2-5 points.
3. **Unverified runs are counted by default.** 76 of 483 have an unparseable
   `verification` from the ADO-200015 defect. Excluding them biases the corpus
   toward post-fix runs, which are also post-prompt-change runs.
   `--require-verified` measures without them.
4. **Tier 2 steps are never scored.** `persistence.py` writes tier-2 output as
   one flat `tier2_findings` string with no per-agent split, so
   `codebase_agent`, `sql_dba_agent`, `auth_security_agent`, `cache_agent` and
   `pubsub_agent` are unattributable. Their steps stay at 0.5 with
   `sample_count: 0` until tier-2 findings are split per agent.
5. **The automatic path never reorders steps.** It writes `hit_rate` and its
   counters, nothing else. Ordering is the SRE's control surface, and an
   optimizer that re-sorts every incident would undo a human reorder within one
   run -- exactly what AC 4 forbids.
6. **An agent listed more than once in one tree is skipped.** `content-archive`
   lists `deploy_correlation_agent` five times as NODE A-F with hand-set rates
   0.9 / 0.8 / 0.7 / 0.85 / 0.8. A per-agent signal cannot tell them apart, so
   applying it would flatten a deliberately tuned tree to one value.

### What the rule cannot see

An agent that correctly rules a hypothesis out, and changes the investigation
without leaving a quotable artifact, scores as a miss. The signal is biased
toward agents that return ids. That bias is why the prior is heavy and why a
human edit outranks any number of samples.

---

## 2. The math

```
hit_rate = clamp( (10 * 0.5 + hits) / (10 + samples), 0.05, 0.95 )
```

A Beta posterior over an explicit prior, not an EWMA. An EWMA has no memory of
how much evidence stands behind its value, so 1 sample and 200 samples produce
numbers that look equally authoritative, and its step size has to be tuned to
trade responsiveness against stability.

- `PRIOR = 0.5` is what every tree already shows, so a tree with no data computes
  to exactly its current value. No discontinuity at rollout.
- `PRIOR_WEIGHT = 10` caps a single incident's movement at `1/11 = 0.045`, which
  satisfies AC 5 by construction rather than by a clamp, and means roughly 10
  samples before measured data outweighs the prior.
- The `[0.05, 0.95]` clamp is belt-and-braces (the prior already keeps the
  posterior inside `(0,1)`) but gives AC 5's floor a number a reader can point
  at. 0.05 also keeps a step visible to any consumer filtering on `hit_rate > 0`.

---

## 3. Schema additions

All optional; every consumer that ignores them keeps working, and
`get_triage_guidance` is unchanged.

| Field | Level | Meaning |
|---|---|---|
| `hit_count`, `sample_count` | step | The evidence behind `hit_rate`. |
| `human_locked` + `_by` / `_at` / `_reason` | step | The automatic path may accumulate counters but must not move `hit_rate`. |
| `optimization_pinned` | tree | The automatic path changes nothing at all. |
| `optimization_log[]` | tree | Append-only audit: `version`, `timestamp`, `actor`, `changes[]`, `incidents_analyzed`, `note`. Capped at 100. |
| `applied_incident_ids[]` | tree | Idempotency guard, capped at 500. |
| `skip_rules[]` | tree | Structured operands (`field` / `op` / `value` / optional `agent`), never expression strings. |

### Every writer merges; none of them replaces the document

All four writers call `txn.update()` with an explicit field dict. None calls
`set()`, and none may start to.

Live trees carry fields none of this code knows about. `monitoring-meta` has
`optimization_note`, the prose record of the 2026-07-30 hand edit, written before
`optimization_log` existed. `content-archive` has `system_model`,
`source_of_truth_order`, `manual_recovery` and `classification`. A whole-document
`set(tree)` would read as an equivalent refactor and would silently delete the
human's explanation of why a tree is tuned the way it is -- the one thing the
audit trail exists to preserve.

Pinned by `TestUnknownFieldsSurvive`, which fails if a writer stops merging.

Those tests run against a fake, so alone they only prove the writers call
`update()` rather than `set()`. That `update()` itself preserves unnamed fields
is a property of google-cloud-firestore 2.28.0 (`uv.lock:1399-1400`):
`pbs_for_update` emits a `Write` whose `update_mask` lists only the supplied
paths. It also defaults to `ExistsOption(exists=True)`, which is why every writer
reads the snapshot first instead of letting the update raise on a missing tree.

### No writer removes a triage step

The automatic path writes `hit_rate` and its counters. It does not prune, and the
correction CLI has no delete-step flag either.

This matters more than it looks. ADO-200023's router intersects its tier-1 agent
list with the tree's `triage_steps`, so deleting a step removes that agent from
every future run of that category with no deploy and no log line at the call site.

**287187's fail-open guard does not cover this.** Read at
`app/tier1_routing.py` on that branch: the `if not selected:` check fires only on
an EMPTY intersection. A PARTIAL prune -- dropping three of ten steps -- is
indistinguishable from deliberate tuning and is obeyed silently. So the guarantee
lives on THIS side, in the writers, not in a downstream safety net. Anyone
arguing that the fail-open makes a `--prune` mode safe has misread it.

Two tripwires, one per path. `test_no_writer_ever_removes_a_triage_step` covers
the automatic path, which never touches the step list.
`test_correction_paths_preserve_the_full_step_set` covers
`apply_engineer_correction`, where it can break: it sorts and renumbers, and a
reorder is where a bad sort key or a dedupe loses an entry. Its fixture lists one
agent three times and it compares as a multiset -- with distinct agents a
dedupe-by-agent prune removes nothing and the test cannot fail.

### Skip rules are operands, not code

`docs/plan-memory-store-integration.md:335` stores skip conditions as Python
source (`"environment == 'development'"`) and calls `eval()` on them at routing
time. That makes any Firestore document with write access an arbitrary-code
execution surface inside the agent. `evaluate_skip_rule` stores the operands
instead and supports `==`, `!=`, `in`, `not_in`. Unknown fields and operators
return `False` -- a malformed rule fails **open** (run the step), because a
skipped step is invisible in the report while a redundant one only costs tokens.

---

## 4. The two paths

### Automatic

`persistence_agent` calls `record_incident_contributions(category, incident_json)`
after the incident document is written, on the in-memory dict it already has.
Transactional, idempotent by `incident_id`, and wrapped so a decision-tree
failure cannot lose the incident that already landed.

### Human -- `scripts/correct_decision_tree.py`

Dry run by default; `--apply` commits. `--actor` and `--reason` are mandatory and
the write is refused without them.

```powershell
# inspect a tree, in the shape the agent reads it
uv run python scripts/correct_decision_tree.py --show monitoring-meta

# demote an agent and lock it against the automatic path
uv run python scripts/correct_decision_tree.py monitoring-meta `
    --actor first.last `
    --reason "known_issues rarely pays off on monitoring alerts" `
    --set-hit-rate known_issues_agent=0.2 --apply

# reorder; a partial list is fine, unnamed steps keep their relative order
uv run python scripts/correct_decision_tree.py sql-database `
    --actor first.last --reason "DBAs always check blocking first" `
    --reorder sql_dba_agent --apply

# skip a step under a condition
uv run python scripts/correct_decision_tree.py cloud-infrastructure `
    --actor first.last --reason "dev has no Datadog agent installed" `
    --add-skip-rule datadog_agent:environment==development --apply

# freeze a hand-tuned tree against automatic changes
uv run python scripts/correct_decision_tree.py content-archive `
    --actor first.last --reason "hand-tuned NODE A-F tree" --pin --apply

# hand a step back to the automatic path
uv run python scripts/correct_decision_tree.py monitoring-meta `
    --actor first.last --reason "enough samples now" `
    --unlock known_issues_agent --apply
```

#### Why a CLI and not an agent tool

The obvious alternative is exposing `apply_engineer_correction` on an LlmAgent so
someone can say "stop checking Teams for monitoring alerts" in chat. Rejected:

1. The tree is the agent's own routing config. An agent that can rewrite its
   routing mid-investigation can talk itself out of checking the source that
   disagrees with it, and the change is attributed to the agent -- `actor` would
   read `ir-agent`, defeating AC 6.
2. Corrections are rare, deliberate and consequential -- closer to a config
   change than a conversational action. They warrant a readable diff and an
   explicit confirmation.
3. It already worked this way once. `monitoring-meta` v2 was a hand edit
   recorded in a prose `optimization_note`. This makes that act repeatable,
   attributed, and structured.

**The trade-off, stated plainly:** a CLI is higher friction than chat, so some
corrections that should be made will not be. Mitigated by `--show`, which prints
the tree in the shape the agent reads it, so an engineer can check a correction's
effect in one command. If adoption is the binding constraint, the next step is a
thin Teams command that calls the same function -- not agent-tool access.

---

## 5. Backfill -- `scripts/mine_decision_tree_hit_rates.py`

Dry run by default. `--apply` requires `--actor`, writes only `decision_trees`,
and skips THIN categories, pinned trees and human-locked steps.

```powershell
uv run python scripts/mine_decision_tree_hit_rates.py --verbose
uv run python scripts/mine_decision_tree_hit_rates.py --category sql-database --verbose
uv run python scripts/mine_decision_tree_hit_rates.py --require-verified
uv run python scripts/mine_decision_tree_hit_rates.py --apply --actor first.last
```

`MIN_SAMPLES = 20` is AC 2's "too thin to support a number" line: at 20 samples
the measured rate carries two thirds of the posterior, so the number is the
corpus talking rather than the prior. THIN categories are printed with their real
numbers and marked, never written.

**A caveat the `--apply` path carries:** a bulk backfill cannot deduplicate the
incidents it absorbed, so it does not touch `applied_incident_ids`. If the live
path later re-processes those same incidents they will be counted twice. The log
entry stamps `backfill: true` and the corpus size so this is visible. Run the
backfill once, before enabling the live path for a category, or accept the
overlap knowingly.

---

## 6. Measured results, 2026-08-20, `aiops-dev-project`

Corpus: 644 incident documents, 483 with a `findings` map, 306 usable after the
verification gate. Note that the minable corpus is smaller than the stored one
and not evenly so -- `sql-database` has 54 documents but only 8 with findings,
`cloud-infrastructure` 78 of which 37; the rest predate `persistence.py` writing
the map.

See the PR description for the full before/after tables. Headline: every
category's learned ordering differs from the current uniform 0.5, and only
`application-errors` (n=66) and `deploy-release` (n=181) clear `MIN_SAMPLES`.
`teams_context_agent` measures 0.07-0.19 across every category with samples.
287187 has since dropped it from every category's selection, so those rates now
record why it went, not a routing input.

---

## 7. What is verified, and what is not

**Verified.**

- The hit rates in section 6: read-only queries over the 644 incident documents
  in `aiops-dev-project`.
- Live tree state: 12 documents, uniform `hit_rate` 0.5 except the two
  hand-edited trees, re-read after every dry run and unchanged.
- Contribution scoring, bounds, idempotency, locks, pinning: unit tests.
- Merge semantics of `update()`, read out of google-cloud-firestore 2.28.0
  (`uv.lock:1399-1400`) down to the `update_mask` in the emitted `Write`.
- Both no-prune tripwires and the persistence guard, each checked by deleting
  the guard and confirming the test fails.

**Not verified.**

- No tree has been written. `record_incident_contributions`,
  `apply_engineer_correction`, `record_backfilled_hit_rates` and the miner's
  `--apply` have only ever run against the in-memory fake or in dry run.
  Nothing is deployed.
- The prune coupling with ADO-200023 is tested on both sides but has never been
  executed through a live router.
- Tier-2 hit rates are unmeasurable until `tier2_findings` is split per agent.

The first live `--apply` is the real test. Run it on one category, with the
before/after diff read by a human, rather than sweeping all twelve.

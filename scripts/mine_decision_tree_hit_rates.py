"""Mine measured decision-tree hit rates from stored incidents.

DRY RUN BY DEFAULT. Writing needs `--apply` and `--actor`, and even then touches
only `decision_trees` -- never `incidents`, GCS, or Agent Engine.

Uses the shipped scorer rather than a copy: a backfill that scores differently
from production would write values production would never produce.

The minable corpus is smaller than the stored one and unevenly so -- documents
written before persistence.py added the `findings` map cannot be scored.
Measured 2026-08-20: sql-database has 54 incidents but 8 with findings,
cloud-infrastructure 78 of which 37.

Usage:
    uv run python scripts/mine_decision_tree_hit_rates.py
    uv run python scripts/mine_decision_tree_hit_rates.py --category sql-database --verbose
    uv run python scripts/mine_decision_tree_hit_rates.py --require-verified
    uv run python scripts/mine_decision_tree_hit_rates.py --apply --actor first.last
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Importing `app` reads the Bifrost gateway settings, required with no default
# since ADO-200022. .invalid cannot resolve, so a stray call fails loudly rather
# than reaching a real environment. Do not copy these into anything that calls.
os.environ.setdefault("BIFROST_MCP_URL", "https://bifrost.invalid/mcp")
os.environ.setdefault("BIFROST_LLM_URL", "https://bifrost.invalid/v1")

from app.tools.decision_tree_learning import (
    PRIOR,
    PRIOR_WEIGHT,
    is_incident_usable,
    posterior_hit_rate,
    score_contributions,
)

# Below this, numbers are printed and marked THIN but never written. At 20
# samples the measured rate carries two thirds of the posterior, so the number
# is the corpus talking rather than the prior (weight 10).
MIN_SAMPLES = 20

_METADATA = os.path.join(os.path.dirname(__file__), "..", "deployment_metadata.json")


def _deployed_target() -> str:
    """Project number from the last deploy recorded in this checkout.

    deployment_metadata.json is rewritten per deploy with no environment key, so
    whichever environment deployed most recently owns it. main() prints the
    resolved target before doing anything.
    """
    try:
        with open(_METADATA, encoding="utf-8") as f:
            return json.load(f)["remote_agent_runtime_id"].split("/")[1]
    except Exception as e:
        raise SystemExit(
            f"cannot read {_METADATA} ({type(e).__name__}: {e}). Pass --project explicitly; "
            f"it is not defaulted because mining the wrong environment's corpus produces a "
            f"table that looks right and describes a different system."
        ) from e


def _project_id(project_number: str) -> str:
    """Translate a project number to its id (Firestore wants the id, not the number)."""
    import subprocess

    out = subprocess.run(
        ["gcloud", "projects", "describe", project_number, "--format=value(projectId)"],
        capture_output=True, text=True, shell=os.name == "nt",
    ).stdout.strip()
    if not out:
        raise SystemExit(
            f"cannot translate project number {project_number} to a project id. "
            f"Pass --project explicitly."
        )
    return out


def mine(project: str, require_verified: bool) -> tuple[dict, dict]:
    """Read every incident and tally (hits, samples) per category per agent.

    Returns (tallies, skipped) where tallies is
    {category: {agent: [hits, samples]}} and skipped is {category: Counter(reason)}.
    Read-only.
    """
    from google.cloud import firestore

    db = firestore.Client(project=project)
    tallies: dict[str, dict[str, list[int]]] = collections.defaultdict(
        lambda: collections.defaultdict(lambda: [0, 0])
    )
    skipped: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for doc in db.collection("incidents").stream():
        data = doc.to_dict() or {}
        category = data.get("category") or "unknown"
        usable, reason = is_incident_usable(data, require_verified=require_verified)
        if not usable:
            skipped[category][reason] += 1
            continue
        contributions = score_contributions(data)
        if not contributions:
            skipped[category]["no attributable tier-1 findings"] += 1
            continue
        for agent, hit in contributions.items():
            tallies[category][agent][0] += 1 if hit else 0
            tallies[category][agent][1] += 1
        skipped[category]["USED"] += 1

    return tallies, skipped


def load_trees(project: str) -> dict[str, dict]:
    from google.cloud import firestore

    db = firestore.Client(project=project)
    return {doc.id: (doc.to_dict() or {}) for doc in db.collection("decision_trees").stream()}


def render(category: str, tree: dict, tally: dict, used: int, verbose: bool) -> list[str]:
    """The before/after table for one category (AC 7)."""
    lines = []
    steps = tree.get("triage_steps") or []
    thin = used < MIN_SAMPLES
    flag = f"  *** THIN (n={used} < {MIN_SAMPLES}) -- reported, NOT written ***" if thin else ""
    lines.append(f"\n{category}  (v{tree.get('version')}, usable incidents n={used}){flag}")
    lines.append(f"  {'agent':30s} {'tier':>4s} {'before':>7s} {'after':>7s} "
                 f"{'hits':>6s} {'n':>5s}  note")
    # An agent listed more than once cannot be attributed per step.
    counts: dict = {}
    for step in steps:
        counts[step.get("agent")] = counts.get(step.get("agent"), 0) + 1
    for step in sorted(steps, key=lambda s: s.get("order", 0)):
        agent = step.get("agent", "?")
        before = step.get("hit_rate")
        observed = tally.get(agent)
        if counts.get(agent, 0) > 1:
            lines.append(f"  {agent:30s} {step.get('tier', '?'):>4} {before:>7} "
                         f"{'--':>7} {'--':>6} {'--':>5}  SKIPPED (listed "
                         f"{counts[agent]}x, not per-step attributable)")
            continue
        if not observed:
            # Tier 2, or a tier-1 agent that never ran for this category.
            note = "unattributable (tier 2)" if step.get("tier") == 2 else "no samples"
            lines.append(f"  {agent:30s} {step.get('tier', '?'):>4} {before:>7} "
                         f"{'--':>7} {'--':>6} {0:>5}  {note}")
            continue
        hits, n = observed
        after = posterior_hit_rate(hits, n)
        note = "HELD (thin)" if thin else ("no change" if after == before else "")
        if step.get("human_locked"):
            note = f"HELD (human_locked by {step.get('human_locked_by')})"
            after = before
        lines.append(f"  {agent:30s} {step.get('tier', '?'):>4} {before:>7} {after:>7.4f} "
                     f"{hits:>6} {n:>5}  {note}")

    if verbose:
        # Only agents that appear exactly once AND have samples. The corpus
        # records findings for all nine regardless of what the tree lists, so
        # ranking an unlisted one makes every category read "ordering differs".
        eligible = {s.get("agent") for s in steps if counts.get(s.get("agent"), 0) == 1}
        scored = {a: posterior_hit_rate(h, n) for a, (h, n) in tally.items() if a in eligible}
        current = [s.get("agent") for s in sorted(steps, key=lambda s: s.get("order", 0))
                   if s.get("agent") in scored]
        learned = [a for a, _ in sorted(scored.items(), key=lambda kv: -kv[1])]
        lines.append(f"  current order : {', '.join(current)}")
        lines.append(f"  learned order : {', '.join(learned)}")
        lines.append(f"  ordering differs: {current != learned}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", default=None,
                        help="Firestore project id. Defaults to the last deploy's project.")
    parser.add_argument("--category", action="append", default=None,
                        help="Limit to these categories (repeatable).")
    parser.add_argument("--require-verified", action="store_true",
                        help="Drop incidents with no readable verification score "
                             "(76 of 483 in the dev corpus, from the ADO-200015 parser defect).")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print current vs learned step ordering per category.")
    parser.add_argument("--apply", action="store_true",
                        help="WRITE the computed hit rates to decision_trees. Requires --actor. "
                             "Skips THIN categories, pinned trees, and human_locked steps.")
    parser.add_argument("--actor", default=None,
                        help="Who is running the backfill. Recorded in optimization_log.")
    args = parser.parse_args()

    project = args.project or _project_id(_deployed_target())
    if args.apply and not args.actor:
        raise SystemExit("--apply requires --actor: an unattributed tree write is what AC 6 forbids.")

    mode = f"APPLY (writes decision_trees as {args.actor})" if args.apply else "DRY RUN (no writes)"
    print(f"{mode}\nTarget: project={project} require_verified={args.require_verified}\n")

    tallies, skipped = mine(project, args.require_verified)
    trees = load_trees(project)

    categories = args.category or sorted(set(trees) | set(tallies))
    total_used = sum(c["USED"] for c in skipped.values())
    print(f"Incidents scanned across all categories: usable={total_used}")

    out: list[str] = []
    writable: list[str] = []
    for category in categories:
        tree = trees.get(category)
        used = skipped[category]["USED"]
        if tree is None:
            out.append(f"\n{category}  -- NO DECISION TREE DOCUMENT "
                       f"(usable incidents n={used}); nothing to update")
            continue
        out.extend(render(category, tree, tallies.get(category, {}), used, args.verbose))
        reasons = {k: v for k, v in skipped[category].items() if k != "USED"}
        if reasons:
            out.append(f"  excluded: {dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}")
        if used >= MIN_SAMPLES and tallies.get(category) and not tree.get("optimization_pinned"):
            writable.append(category)
    print("\n".join(out))

    print(f"\nPrior: hit_rate = ({PRIOR_WEIGHT}*{PRIOR} + hits) / ({PRIOR_WEIGHT} + n), "
          f"clamped to [0.05, 0.95].")
    print(f"Categories with n >= {MIN_SAMPLES} and an unpinned tree: "
          f"{', '.join(writable) if writable else 'none'}")

    if not args.apply:
        print("\nNo writes were made. Re-run with --apply --actor <you> to write the "
              "categories listed above; THIN categories and human_locked steps are "
              "skipped even then.")
        return 0

    # Before the import: _get_db() reads GOOGLE_CLOUD_PROJECT with no default,
    # and this must write to the environment it just measured.
    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    from app.tools.firestore_tools import record_backfilled_hit_rates

    for category in writable:
        tally = {a: (h, n) for a, (h, n) in tallies[category].items()}
        result = record_backfilled_hit_rates(
            category, json.dumps(tally), skipped[category]["USED"], args.actor
        )
        print(f"  {category}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

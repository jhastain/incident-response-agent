"""Engineer-facing correction path for decision trees. DRY RUN BY DEFAULT.

An SRE who watched an investigation go wrong corrects the tree here. This is the
primary control surface: the automatic path moves a hit rate by at most 0.045
per incident and cannot see an agent whose value was ruling something OUT.

Deliberately a CLI and not an agent tool. The tree is the agent's own routing
config, so an agent able to rewrite it can talk itself out of checking a source
that disagrees, and `actor` would read "ir-agent". The cost is friction, so some
corrections will not get made; `--show` prints the tree as the agent reads it.

Usage:
    uv run python scripts/correct_decision_tree.py --show monitoring-meta

    uv run python scripts/correct_decision_tree.py monitoring-meta \\
        --actor first.last --reason "known_issues rarely pays off here" \\
        --set-hit-rate known_issues_agent=0.2

    uv run python scripts/correct_decision_tree.py sql-database \\
        --actor first.last --reason "DBAs always check blocking first" \\
        --reorder sql_dba_agent,gcp_logging_agent

    uv run python scripts/correct_decision_tree.py cloud-infrastructure \\
        --actor first.last --reason "dev has no Datadog agent installed" \\
        --add-skip-rule datadog_agent:environment==development

    uv run python scripts/correct_decision_tree.py content-archive \\
        --actor first.last --reason "hand-tuned NODE A-F tree" --pin

    ... --apply     # to write any of the above
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Importing `app` reads the Bifrost gateway settings, required with no default
# since ADO-200022. .invalid cannot resolve, so a stray call fails loudly.
os.environ.setdefault("BIFROST_MCP_URL", "https://bifrost.invalid/mcp")
os.environ.setdefault("BIFROST_LLM_URL", "https://bifrost.invalid/v1")

_METADATA = os.path.join(os.path.dirname(__file__), "..", "deployment_metadata.json")


def _project_id() -> str:
    import subprocess

    with open(_METADATA, encoding="utf-8") as f:
        number = json.load(f)["remote_agent_runtime_id"].split("/")[1]
    out = subprocess.run(
        ["gcloud", "projects", "describe", number, "--format=value(projectId)"],
        capture_output=True, text=True, shell=os.name == "nt",
    ).stdout.strip()
    if not out:
        raise SystemExit(f"cannot resolve project id from number {number}. Pass --project.")
    return out


def _parse_hit_rate(text: str) -> dict:
    agent, _, value = text.partition("=")
    if not agent or not value:
        raise SystemExit(f"--set-hit-rate wants agent=0.25, got {text!r}")
    return {"agent": agent.strip(), "hit_rate": float(value)}


def _parse_action(text: str) -> dict:
    agent, _, action = text.partition("=")
    if not agent or not action:
        raise SystemExit(f"--set-action wants agent=new action text, got {text!r}")
    return {"agent": agent.strip(), "action": action.strip()}


def _parse_skip_rule(text: str) -> dict:
    """`agent:field==value`, or `field==value` for a category-wide rule.

    Stored as operands, not an expression: the design in
    plan-memory-store-integration.md eval()d stored Python source at routing
    time, making any Firestore write arbitrary code execution.
    """
    agent = None
    body = text
    if ":" in text and "==" in text and text.index(":") < text.index("="):
        agent, _, body = text.partition(":")
    for op in ("!=", "=="):
        if op in body:
            field, _, value = body.partition(op)
            rule = {"field": field.strip(), "op": op, "value": value.strip()}
            if agent:
                rule["agent"] = agent.strip()
            return rule
    raise SystemExit(f"--add-skip-rule wants [agent:]field==value or field!=value, got {text!r}")


def _show(tree: dict) -> str:
    lines = [
        f"category={tree.get('category')} version={tree.get('version')} "
        f"incident_count={tree.get('incident_count')} "
        f"pinned={bool(tree.get('optimization_pinned'))}",
        f"last_optimized={tree.get('last_optimized')}",
        f"  {'#':>2} {'agent':30s} {'t':>2} {'hit_rate':>8s} {'n':>5s}  action / lock",
    ]
    for step in sorted(tree.get("triage_steps") or [], key=lambda s: s.get("order", 0)):
        lock = ""
        if step.get("human_locked"):
            lock = f"  [LOCKED by {step.get('human_locked_by')}: {step.get('human_locked_reason')}]"
        lines.append(
            f"  {step.get('order', '?'):>2} {step.get('agent', '?'):30s} "
            f"{step.get('tier', '?'):>2} {step.get('hit_rate'):>8} "
            f"{step.get('sample_count', 0):>5}  {step.get('action', '')}{lock}"
        )
    for rule in tree.get("skip_rules") or []:
        lines.append(f"  skip_rule: {json.dumps(rule, default=str)}")
    for entry in (tree.get("optimization_log") or [])[-5:]:
        lines.append(
            f"  log v{entry.get('version')} {entry.get('timestamp')} "
            f"{entry.get('actor')}: {'; '.join(entry.get('changes') or [])[:200]}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("category", nargs="?", help="The decision tree to correct.")
    parser.add_argument("--project", default=None)
    parser.add_argument("--show", metavar="CATEGORY", default=None,
                        help="Print a tree and exit. No other flags needed.")
    parser.add_argument("--actor", default=None, help="Your username. Recorded in optimization_log.")
    parser.add_argument("--reason", default=None, help="Why. Recorded in optimization_log.")
    parser.add_argument("--reorder", default=None,
                        help="Comma-separated agent names. Partial lists are fine.")
    parser.add_argument("--set-hit-rate", action="append", default=[], metavar="AGENT=RATE",
                        help="Set and human-lock a step's hit rate.")
    parser.add_argument("--set-action", action="append", default=[], metavar="AGENT=TEXT")
    parser.add_argument("--unlock", action="append", default=[], metavar="AGENT",
                        help="Return a step to the automatic path.")
    parser.add_argument("--add-skip-rule", action="append", default=[],
                        metavar="[AGENT:]FIELD==VALUE")
    parser.add_argument("--remove-skip-rule", action="append", default=[],
                        metavar="[AGENT:]FIELD==VALUE")
    parser.add_argument("--pin", action="store_true", help="Stop automatic hit-rate updates.")
    parser.add_argument("--unpin", action="store_true", help="Resume automatic updates.")
    parser.add_argument("--apply", action="store_true", help="Actually write. Default is dry run.")
    args = parser.parse_args()

    project = args.project or _project_id()
    os.environ["GOOGLE_CLOUD_PROJECT"] = project

    from google.cloud import firestore

    db = firestore.Client(project=project)

    if args.show:
        snapshot = db.collection("decision_trees").document(args.show).get()
        if not snapshot.exists:
            raise SystemExit(f"no decision tree '{args.show}' in {project}")
        print(_show(snapshot.to_dict() or {}))
        return 0

    if not args.category:
        parser.error("a category is required (or use --show CATEGORY)")
    if not args.actor or not args.reason:
        parser.error("--actor and --reason are required: an unattributed tree edit is what AC 6 forbids")
    if args.pin and args.unpin:
        parser.error("--pin and --unpin are mutually exclusive")

    correction: dict = {"actor": args.actor, "reason": args.reason}
    if args.reorder:
        correction["reorder"] = [a.strip() for a in args.reorder.split(",") if a.strip()]
    if args.set_hit_rate:
        correction["set_hit_rate"] = [_parse_hit_rate(t) for t in args.set_hit_rate]
    if args.set_action:
        correction["set_action"] = [_parse_action(t) for t in args.set_action]
    if args.unlock:
        correction["unlock"] = args.unlock
    if args.add_skip_rule:
        correction["add_skip_rule"] = [_parse_skip_rule(t) for t in args.add_skip_rule]
    if args.remove_skip_rule:
        correction["remove_skip_rule"] = [_parse_skip_rule(t) for t in args.remove_skip_rule]
    if args.pin:
        correction["pinned"] = True
    if args.unpin:
        correction["pinned"] = False

    snapshot = db.collection("decision_trees").document(args.category).get()
    if not snapshot.exists:
        raise SystemExit(f"no decision tree '{args.category}' in {project}")

    print(f"Target: project={project} category={args.category}")
    print("\n--- BEFORE ---")
    print(_show(snapshot.to_dict() or {}))
    print("\n--- CORRECTION ---")
    print(json.dumps(correction, indent=2))

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to commit this correction.")
        return 0

    from app.tools.firestore_tools import apply_engineer_correction

    result = json.loads(apply_engineer_correction(args.category, json.dumps(correction)))
    print("\n--- RESULT ---")
    print(json.dumps(result, indent=2))
    if result.get("error"):
        return 1

    print("\n--- AFTER ---")
    print(_show(db.collection("decision_trees").document(args.category).get().to_dict() or {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

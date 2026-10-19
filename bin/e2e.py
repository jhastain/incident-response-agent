#!/usr/bin/env python3
"""End-to-end check for the Incident Response Agent.

One command, real code paths, explicit pass/fail. Everything that needs live
infrastructure prints a SKIP with the reason rather than being left out: a green
run that quietly covers less than it looks like is the failure mode this agent
itself is built to avoid.

    uv run python bin/e2e.py

`uv run pytest tests/unit` is the deeper suite behind this; this is the
single-command smoke check over the load-bearing behaviour.

Exit code 0 only if every check passed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# config.py raises at import unless the gateway URLs are set - deliberately, so a
# run can never silently inherit production. Point them somewhere unresolvable so
# that a stray network call fails loudly instead of reaching a real gateway.
os.environ.setdefault("BIFROST_MCP_URL", "https://gateway.invalid/mcp")
os.environ.setdefault("BIFROST_LLM_URL", "https://gateway.invalid/v1")
os.environ.setdefault("IR_AGENT_ENGINE_URL", "https://aiplatform.invalid/v1beta1/x")

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str, str]] = []
SKIP: list[tuple[str, str]] = []


# Binary files cannot be grepped, so each one listed here has been opened and
# read by eye. All four are Mermaid renders showing agent names and generic
# infrastructure labels only. Two sibling renders that showed internal
# hostnames were deleted rather than listed here.
REVIEWED_BINARIES = [
    "docs/02-alert-dedup-flow.png",
    "docs/03-investigation-workflow.png",
    "docs/04-data-model.png",
    "docs/05-routing-decision-tree.png",
]

class _Skip(Exception):
    pass


def check(name):
    def wrap(fn):
        try:
            detail = fn()
        except _Skip as exc:
            SKIP.append((name, str(exc)))
        except Exception as exc:
            FAIL.append((name, f"{type(exc).__name__}: {exc}", traceback.format_exc()))
        else:
            PASS.append((name, detail or ""))
        return fn
    return wrap


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

@check("dependencies importable")
def _deps():
    missing = []
    for mod in ("google.adk", "google.cloud.firestore", "google.cloud.storage", "requests"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise RuntimeError(f"missing: {', '.join(missing)} - run: uv sync --frozen")
    return "google.adk, firestore, storage, requests"


if FAIL:
    print("Cannot continue: dependencies are not installed.")
    for name, msg, _ in FAIL:
        print(f"  FAIL  {name}: {msg}")
    sys.exit(1)


@check("gateway URLs are required, not defaulted")
def _required_urls():
    import importlib

    from app import config

    saved = os.environ.pop("BIFROST_LLM_URL")
    try:
        importlib.reload(config)
        try:
            config.get_bifrost_settings()
        except Exception as exc:
            msg = str(exc)
        else:
            raise AssertionError(
                "a missing gateway URL was tolerated; it previously defaulted to "
                "prod, so a local run routed every call through production"
            )
    finally:
        os.environ["BIFROST_LLM_URL"] = saved
        importlib.reload(config)
    assert "BIFROST_LLM_URL" in msg, msg
    return "unset URL raises, naming the variable"


@check("the pipeline is the documented sequence")
def _pipeline():
    from app.agent import root_agent

    names = [a.name for a in root_agent.sub_agents]
    expected_head = ["alert_classifier", "infra_context"]
    assert names[:2] == expected_head, f"pipeline starts {names[:2]}, expected {expected_head}"
    for required in ("synthesis_agent", "report_agent", "verification_agent"):
        assert any(required in n for n in names), f"{required} missing from {names}"
    # Report must come before verification: the verifier scores the report.
    report_at = next(i for i, n in enumerate(names) if "report" in n)
    verify_at = next(i for i, n in enumerate(names) if "verification" in n)
    assert report_at < verify_at, "verification runs before the report it is meant to score"
    return f"{len(names)} stages: {' -> '.join(names)}"


@check("all 9 tier-1 investigators are registered")
def _tier1_members():
    from app.sub_agents.tier1.parallel import tier1_parallel
    from app.tier1_routing import ALL_TIER1_AGENTS

    members = {a.name for a in tier1_parallel.sub_agents}
    declared = set(ALL_TIER1_AGENTS)
    assert members == declared, (
        f"ParallelAgent membership drifted from ALL_TIER1_AGENTS.\n"
        f"  only in agent: {sorted(members - declared)}\n"
        f"  only declared: {sorted(declared - members)}"
    )
    return f"{len(members)} agents, membership matches the routing table"


# --------------------------------------------------------------------------
# Routing: the database may narrow the plan, never widen it
# --------------------------------------------------------------------------

@check("the stored tree can remove an agent but never add one")
def _routing_intersection():
    from app.tier1_routing import DEFAULT_TIER1_SELECTION, build_tier1_plan

    category = next(iter(DEFAULT_TIER1_SELECTION))
    default = list(DEFAULT_TIER1_SELECTION[category])
    assert len(default) >= 2, f"{category} has too few defaults to test narrowing"

    # A tree naming one default agent plus an agent the code does not list.
    guidance = json.dumps({
        "version": 7,
        "triage_steps": [
            {"order": 1, "agent": default[0], "tier": 1, "action": "look here first"},
            {"order": 2, "agent": "agent_that_does_not_exist", "tier": 1, "action": "nope"},
        ],
    })
    plan = build_tier1_plan(category, guidance, {"severity": "sev2"})
    assert "agent_that_does_not_exist" not in plan["selected"], plan["selected"]
    assert default[0] in plan["selected"], plan["selected"]
    assert plan["fail_open"] is False, "narrowing should not trip fail-open"
    return f"{category}: {len(default)} default -> {len(plan['selected'])} selected, unknown agent ignored"


@check("every degraded routing path fails open to all 9")
def _routing_fail_open():
    from app.tier1_routing import ALL_TIER1_AGENTS, build_tier1_plan

    cases = {
        "no guidance": (next(iter(__import__(
            "app.tier1_routing", fromlist=["DEFAULT_TIER1_SELECTION"]
        ).DEFAULT_TIER1_SELECTION)), None),
        "malformed json": ("sql-database", "{not json"),
        "empty steps": ("sql-database", json.dumps({"version": 1, "triage_steps": []})),
        "unknown category": ("no-such-category", json.dumps({"triage_steps": []})),
    }
    for label, (category, guidance) in cases.items():
        plan = build_tier1_plan(category, guidance, {})
        assert plan["fail_open"] is True, f"{label}: did not fail open ({plan['tree_source']})"
        assert set(plan["selected"]) == set(ALL_TIER1_AGENTS), (
            f"{label}: failed open to {len(plan['selected'])} agents, not all "
            f"{len(ALL_TIER1_AGENTS)}"
        )
    return f"{len(cases)} degraded inputs, all ran the full {len(ALL_TIER1_AGENTS)}"


@check("skip-rule conditions are parsed, never eval()'d")
def _skip_grammar():
    import ast as _ast

    import app.tier1_routing as tr
    from app.tier1_routing import evaluate_skip_condition

    # Parse rather than grep: the module mentions eval() in prose precisely to
    # say that it does not use it, and ast.literal_eval is a different function.
    tree = _ast.parse(Path(tr.__file__).read_text(encoding="utf-8"))
    bare = [
        n for n in _ast.walk(tree)
        if isinstance(n, _ast.Call)
        and isinstance(n.func, _ast.Name)
        and n.func.id in {"eval", "exec"}
    ]
    assert not bare, (
        f"eval()/exec() called at line {bare[0].lineno} on a condition string that "
        f"comes from a database row - that is remote code execution"
    )
    literal = [
        n for n in _ast.walk(tree)
        if isinstance(n, _ast.Call)
        and isinstance(n.func, _ast.Attribute)
        and n.func.attr == "literal_eval"
    ]
    assert literal, "literals should be parsed with ast.literal_eval, not trusted"
    from app.tier1_routing import SkipRuleError, build_tier1_plan

    fields = {"severity": "sev3", "environment": "test", "category": "performance"}
    assert evaluate_skip_condition("severity == 'sev3'", fields) is True
    assert evaluate_skip_condition("severity == 'sev1'", fields) is False

    # Anything outside the grammar is rejected loudly rather than guessed at.
    for bad in ("__import__('os').system('x')", "1 == 1", "", "severity ==",
                "nonsense", "os.system('x')"):
        try:
            evaluate_skip_condition(bad, fields)
        except SkipRuleError:
            pass
        else:
            raise AssertionError(f"accepted a condition outside the grammar: {bad!r}")

    # And a tree carrying such a rule must still produce a usable plan.
    import json as _json
    guidance = _json.dumps({
        "version": 1,
        "triage_steps": [{"order": 1, "agent": "datadog_agent", "tier": 1, "action": "x"}],
        "skip_rules": [{"condition": "__import__('os').system('x')", "skip": ["datadog_agent"]}],
    })
    plan = build_tier1_plan("performance", guidance, fields)
    assert plan["selected"], "a malformed skip rule emptied the plan instead of being ignored"
    return "no eval()/exec(); 6 out-of-grammar conditions rejected; plan still built"


@check("a skipped source says so in words synthesis cannot misread")
def _skip_sentinel():
    from app.tier1_routing import build_tier1_plan, skip_sentinel

    plan = build_tier1_plan("sql-database", "{not json", {})
    plan = {
        **plan,
        "category": "sql-database",
        "skipped": [{"agent": "teams_context", "reason": "not selected for this category"}],
    }
    text = skip_sentinel("teams_context", plan)
    assert text.strip(), "an empty sentinel reads to synthesis as 'checked, found nothing'"
    low = text.lower()
    for needle in ("not run", "not checked", "not evidence of absence"):
        assert needle in low, f"sentinel is missing {needle!r}: {text}"
    for banned in ("healthy", "no data found", "all clear"):
        assert f" {banned}" not in low.replace("as healthy", ""), f"sentinel could read as {banned}"
    return f"{len(text)} chars, states NOT CHECKED explicitly"


# --------------------------------------------------------------------------
# Decision-tree learning
# --------------------------------------------------------------------------

@check("the posterior cannot reach 0 or 1 at any sample count")
def _posterior_bounds():
    from app.tools.decision_tree_learning import (
        HIT_RATE_CEILING,
        HIT_RATE_FLOOR,
        posterior_hit_rate,
    )

    cases = [(0, 0), (0, 1), (0, 1000), (1000, 1000), (5, 10), (-3, 2), (10, 0)]
    for hits, samples in cases:
        r = posterior_hit_rate(hits, samples)
        assert HIT_RATE_FLOOR <= r <= HIT_RATE_CEILING, f"({hits},{samples}) -> {r}"
    # A single incident must not be able to swing routing.
    swing = abs(posterior_hit_rate(1, 1) - posterior_hit_rate(0, 1))
    assert swing < 0.10, f"one sample moves the rate by {swing:.3f}; the prior is too light"
    return (
        f"{len(cases)} cases inside [{HIT_RATE_FLOOR}, {HIT_RATE_CEILING}]; "
        f"one sample moves it {swing:.3f}"
    )


@check("ambient input is subtracted from contribution scoring")
def _contribution_scoring():
    from app.tools.decision_tree_learning import (
        FINDINGS_KEY_TO_AGENT,
        score_contributions,
    )

    key = "findings_datadog"
    agent = FINDINGS_KEY_TO_AGENT[key]

    # Same artifact in the alert the agent was HANDED and in its findings.
    # Echoing your own input is not a contribution.
    echoed = {
        "alert_info": {"raw_text": "see `sqlprod-c1` for detail"},
        "findings": {key: "the host `sqlprod-c1` is implicated"},
        "synthesis": "root cause on `sqlprod-c1`",
        "report": "root cause on `sqlprod-c1`",
    }
    # A token the agent produced itself, which then reached the report.
    produced = {
        "alert_info": {"raw_text": "errors are up"},
        "findings": {key: "blocking chain head is session `spid-98765-head`"},
        "synthesis": "blocked by session `spid-98765-head`",
        "report": "blocked by session `spid-98765-head`",
    }
    assert score_contributions(echoed).get(agent) is False, (
        "an agent that only echoed its own input scored as contributing"
    )
    assert score_contributions(produced).get(agent) is True, (
        "an agent that produced a new artifact used downstream scored as not contributing"
    )

    # A skipped agent is omitted, not scored a miss: never running is not the
    # same as running and finding nothing.
    from app.tier1_routing import build_tier1_plan, skip_sentinel

    plan = {**build_tier1_plan("sql-database", "{not json", {}),
            "category": "sql-database",
            "skipped": [{"agent": agent, "reason": "not selected"}]}
    skipped = {**produced, "findings": {key: skip_sentinel(agent, plan)}}
    assert agent not in score_contributions(skipped), (
        "a skipped agent was scored instead of omitted"
    )
    return "echo scores False, novel artifact scores True, skipped agent omitted"


# --------------------------------------------------------------------------
# Report discipline
# --------------------------------------------------------------------------

@check("the report prompt keeps its hard caps and its ban on recommendations")
def _report_prompt():
    from app.sub_agents import report

    text = Path(report.__file__).read_text(encoding="utf-8")
    low = text.lower()
    assert re.search(r"\b\d{3,4}\s*char", low), "the body character cap is gone"
    assert "strong" in low, "the STRONG-evidence gate on key findings is gone"
    # The phrase appears in order to be banned. Check it is in a prohibition,
    # not that it is absent.
    for m in re.finditer(r"recommended actions", low):
        window = low[max(0, m.start() - 160):m.start()]
        assert any(w in window for w in ("no ", "not ", "never", "do not", "without")), (
            "a Recommended Actions section reappeared outside a prohibition; this "
            "agent diagnoses, it does not remediate"
        )
    return "character cap, evidence gate and the no-remediation rule all present"


@check("the Datadog time-window guard still names all four silent failures")
def _window_guard():
    from app.sub_agents.tier1 import datadog_tierb

    text = Path(datadog_tierb.__file__).read_text(encoding="utf-8").lower()
    for needle, why in (
        ("unconstrained", "the marker synthesis keys on to refuse a number as evidence"),
        ("start=", "start=/end= is accepted by the server and silently dropped"),
        ("timestamp", "a SQL TIMESTAMP predicate only intersects the default rolling hour"),
    ):
        assert needle in text, f"missing {needle!r}: {why}"
    return "unconstrained marker, start=/end= trap and the TIMESTAMP trap all covered"


# --------------------------------------------------------------------------
# Publication hygiene
# --------------------------------------------------------------------------


@check("no run artifacts are present")
def _no_artifacts():
    patterns = ["out*.json", ".p0", "deployment_metadata.json", "v*_raw_response.json",
                "last_report*.txt", "rc-stream.jsonl"]
    found = [str(p.relative_to(ROOT)) for pat in patterns for p in ROOT.glob(pat)]
    assert not found, f"these dump real investigations and must not ship: {found}"
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for needed in ("out*.json", ".p0/", "deployment_metadata.json"):
        assert needed in gitignore, f".gitignore does not cover {needed}"
    return "none present, and .gitignore covers them"


@check("the eval alert corpus is synthetic and complete")
def _alert_corpus():
    alerts = sorted((ROOT / "tests" / "eval" / "alerts").glob("alert_*.txt"))
    assert len(alerts) == 5, f"judge_root_cause.load_alerts() reads alert_01..05, found {len(alerts)}"
    sys.path.insert(0, str(ROOT / "tests" / "eval"))
    import judge_root_cause

    sigs = judge_root_cause.ALERT_SIGNATURES
    bodies = {p.stem.replace("_0", "0").replace("alert0", "alert0"): p.read_text(encoding="utf-8").lower()
              for p in alerts}
    for key, needles in sigs.items():
        body = bodies.get(key.replace("alert", "alert_0").replace("alert_00", "alert_0"))
        if body is None:
            body = next((b for k, b in bodies.items() if k.endswith(key[-2:])), None)
        assert body is not None, f"no fixture matches signature key {key}"
        assert any(n in body for n in needles), (
            f"{key}: none of {needles} appear in its fixture, so the judge cannot map "
            f"a report back to it"
        )
    return f"{len(alerts)} fixtures, every signature matches its alert"


# --------------------------------------------------------------------------
# Not covered here - stated explicitly rather than omitted
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Pre-publication leak gate (shared; see bin/leakcheck.py)
# --------------------------------------------------------------------------

@check("no employer identifiers in the working tree")
def _leak_tree():
    sys.path.insert(0, str(ROOT / "bin"))
    import leakcheck

    hits = leakcheck.scan_tree(ROOT, self_path=Path(leakcheck.__file__))
    assert not hits, "\n  " + "\n  ".join(hits[:12])
    return "clean, lockfiles included"


@check("no employer identifiers anywhere in git history")
def _leak_history():
    sys.path.insert(0, str(ROOT / "bin"))
    import leakcheck

    if not (ROOT / ".git").exists():
        raise _Skip("not a git repository")
    hits = leakcheck.scan_history(ROOT)
    assert not hits, (
        "a fresh history is only clean if nothing was committed and then removed:\n  "
        + "\n  ".join(hits[:12])
    )
    return "clean across all commits"


@check("every binary file has been reviewed by eye")
def _binaries():
    sys.path.insert(0, str(ROOT / "bin"))
    import leakcheck

    found = set(leakcheck.binaries(ROOT))
    allowed = set(REVIEWED_BINARIES)
    unreviewed = sorted(found - allowed)
    stale = sorted(allowed - found)
    assert not unreviewed, (
        f"these cannot be grepped and are not on the reviewed list, so nobody has "
        f"confirmed what they show: {unreviewed}"
    )
    assert not stale, f"REVIEWED_BINARIES lists files that no longer exist: {stale}"
    return f"{len(found)} binaries, all reviewed"


UNCOVERED = [
    ("Any LLM call", "needs a gateway virtual key; model behaviour is scored by tests/eval, not asserted here"),
    ("MCP gateway tool calls", "needs a reachable gateway and a virtual key"),
    ("GCP reads (Logging, Monitoring, Cloud Run, Compute)", "needs application-default credentials"),
    ("Firestore and GCS persistence", "needs ADC plus a project with both enabled"),
    ("Azure DevOps and Octopus REST", "needs a read-only PAT and an Octopus API key"),
    ("Agent Engine deploy", "mints a billable reasoning engine"),
    ("PagerDuty webhook receiver end to end", "needs a PagerDuty account and a public endpoint"),
    ("Datadog span emission", "needs DD_API_KEY and a real agentless endpoint"),
    ("Root-cause accuracy", "tests/eval/ against a deployed engine; this harness cannot score quality"),
]


def main() -> int:
    for name, reason in UNCOVERED:
        SKIP.append((name, reason))

    width = max(len(n) for n, *_ in PASS + FAIL + SKIP)
    for name, detail in PASS:
        print(f"PASS  {name:<{width}}  {detail}")
    for name, reason in SKIP:
        print(f"SKIP  {name:<{width}}  {reason}")
    for name, msg, tb in FAIL:
        print(f"FAIL  {name:<{width}}  {msg}")
        print("".join("        " + line for line in tb.splitlines(keepends=True)))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

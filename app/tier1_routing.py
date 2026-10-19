"""Tier 1 routing: the category's decision tree picks which Tier 1 agents run.

Selection intersects DEFAULT_TIER1_SELECTION (in-repo) with the tier-1 steps the
Firestore tree names. Deleting a step retunes a run with no deploy; Firestore
cannot add an agent the repo does not list, because an unresolvable agent name is
a typo rather than a routing instruction.

DEFAULT_TIER1_SELECTION is hand-authored because the seed data cannot order
anything: 11 of 12 live trees list every agent at hit_rate 0.5. ADO-200024
replaces it per-category as each earns data -- not wholesale, and not with a
threshold near 0.5. Measured hit rates run far lower (teams_context 0.073 on
deploy-release, n=181), so a 0.5 cut would drop five of nine agents, and only
deploy-release and application-errors currently clear the sample floor.

A PARTIAL prune of a tree is obeyed silently -- it is indistinguishable from
deliberate tuning. Only an EMPTY intersection fails open. Nothing prunes today;
if something starts to, note the asymmetry: adding an agent needs a deploy,
removing one is a Firestore edit.

Every failure mode -- unreachable Firestore, unknown category, malformed doc,
empty intersection, unparseable skip rule -- runs all 9 and records why (AC 7).
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

# synthesis.py and report.py read these keys directly. An unset key renders as ""
# and reads as "checked, found nothing", hence the skip sentinel (AC 5).
TIER1_AGENT_FINDINGS_KEYS: dict[str, str] = {
    "cloud_run_health": "findings_cloud_run",
    "compute_engine_health": "findings_compute_engine",
    "gcp_logging": "findings_gcp_logging",
    "gcp_metrics": "findings_gcp_metrics",
    "datadog_observability": "findings_datadog",
    "sentry_error": "findings_sentry",
    "deploy_correlation": "findings_deploy",
    "teams_context": "findings_teams",
    "known_issues": "findings_known_issues",
}

ALL_TIER1_AGENTS: tuple[str, ...] = tuple(TIER1_AGENT_FINDINGS_KEYS)

# Flat keys, one per agent (AC 3): ADK's inject_session_state resolves only
# top-level `{name}` references and cannot index into a dict.
TIER1_FOCUS_KEY_PREFIX = "tier1_focus_"

# Tree documents use the pre-Tier-B `*_agent` names; the ADK agents kept their
# old names deliberately. Anything absent here is a tier-2 or historical_context
# step and is ignored, not treated as unknown.
TREE_AGENT_TO_ADK_AGENT: dict[str, str] = {
    "gcp_logging_agent": "gcp_logging",
    "gcp_metrics_agent": "gcp_metrics",
    "datadog_agent": "datadog_observability",
    "sentry_agent": "sentry_error",
    "cloud_run_health_agent": "cloud_run_health",
    "deploy_correlation_agent": "deploy_correlation",
    "known_issues_agent": "known_issues",
    "teams_context_agent": "teams_context",
    "compute_engine_health_agent": "compute_engine_health",
}

# Every list MUST be non-empty (AC 8).
#
# teams_context is excluded everywhere: its prompt reports "Teams context
# unavailable" and makes no tool calls, so it cannot return evidence.
# compute_engine_health is excluded unless the category can involve VMs or MIGs;
# its prompt short-circuits with "Not applicable" otherwise.
DEFAULT_TIER1_SELECTION: dict[str, list[str]] = {
    "application-errors": [
        "sentry_error", "gcp_logging", "datadog_observability",
        "deploy_correlation", "cloud_run_health", "known_issues",
    ],
    "business-logic": [
        "sentry_error", "gcp_logging", "datadog_observability",
        "deploy_correlation", "known_issues",
    ],
    "deploy-release": [
        "deploy_correlation", "gcp_logging", "cloud_run_health",
        "datadog_observability", "sentry_error", "known_issues",
    ],
    "scheduled-jobs": [
        "gcp_logging", "datadog_observability", "sentry_error",
        "deploy_correlation", "known_issues", "compute_engine_health",
    ],
    "data-integrity": [
        "gcp_logging", "sentry_error", "datadog_observability",
        "deploy_correlation", "known_issues",
    ],
    "cloud-infrastructure": [
        "cloud_run_health", "gcp_metrics", "gcp_logging",
        "compute_engine_health", "datadog_observability", "deploy_correlation",
    ],
    "sql-database": [
        "datadog_observability", "gcp_logging", "gcp_metrics",
        "sentry_error", "deploy_correlation", "known_issues",
    ],
    "auth-security": [
        "gcp_logging", "datadog_observability", "sentry_error",
        "deploy_correlation", "known_issues", "cloud_run_health",
    ],
    "performance": [
        "datadog_observability", "gcp_metrics", "gcp_logging",
        "cloud_run_health", "sentry_error", "deploy_correlation",
    ],
    "monitoring-meta": [
        "gcp_metrics", "datadog_observability", "gcp_logging",
        "cloud_run_health", "sentry_error", "known_issues",
        "deploy_correlation",
    ],
    "pubsub-messaging": [
        "gcp_logging", "gcp_metrics", "datadog_observability",
        "sentry_error", "deploy_correlation", "cloud_run_health",
    ],
    # The tree's own tier-1 steps name only these three.
    "content-archive": ["deploy_correlation", "datadog_observability", "known_issues"],
    # No tree document exists and there is no basis for narrowing.
    "unknown": list(ALL_TIER1_AGENTS),
}


class SkipRuleError(ValueError):
    """A skip rule could not be parsed. The rule is ignored, never guessed at."""


# `<field> <op> <literal>`, with the operator one of four fixed tokens. The field
# is checked against SKIP_RULE_FIELDS, never treated as an expression.
_CONDITION_RE = re.compile(
    r"^\s*(?P<field>[a-z_][a-z0-9_]*)\s+(?P<op>==|!=|not\s+in|in)\s+(?P<literal>.+?)\s*$",
    re.IGNORECASE,
)

# AC 4 names severity and environment; the other two are stable classifier
# outputs, so a rule naming one is intentional. Anything else raises.
SKIP_RULE_FIELDS: frozenset[str] = frozenset(
    {"severity", "environment", "category", "service_name"}
)


def evaluate_skip_condition(condition: str, fields: dict[str, Any]) -> bool:
    """True if the condition holds, meaning the step is skipped.

    NOT eval(): these strings come from Firestore rows, so eval() would be code
    execution reachable by editing a database row. The whole grammar is
    `<field> ==|!= <str>` and `<field> in|not in <list-of-str>`, field from
    SKIP_RULE_FIELDS, literal via ast.literal_eval. Comparison is
    case-insensitive -- the classifier emits "prod" but a rule may say "Prod".

    Raises SkipRuleError on anything that does not parse.
    """
    match = _CONDITION_RE.match(condition or "")
    if not match:
        raise SkipRuleError(f"unparseable condition: {condition!r}")

    field = match.group("field").lower()
    if field not in SKIP_RULE_FIELDS:
        raise SkipRuleError(
            f"condition names unknown field {field!r}; allowed: "
            f"{sorted(SKIP_RULE_FIELDS)}"
        )

    # Collapse "not  in" -> "not in" so the operator is a fixed token.
    op = re.sub(r"\s+", " ", match.group("op").strip().lower())

    try:
        literal = ast.literal_eval(match.group("literal"))
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise SkipRuleError(
            f"condition literal is not a Python literal: {match.group('literal')!r}"
        ) from exc

    actual = str(fields.get(field, "") or "").strip().lower()

    if op in ("==", "!="):
        if not isinstance(literal, str):
            raise SkipRuleError(f"{op} requires a string literal, got {type(literal).__name__}")
        equal = actual == literal.strip().lower()
        return equal if op == "==" else not equal

    if not isinstance(literal, (list, tuple, set, frozenset)):
        raise SkipRuleError(f"'{op}' requires a list literal, got {type(literal).__name__}")
    if not all(isinstance(item, str) for item in literal):
        raise SkipRuleError(f"'{op}' list must contain only strings: {literal!r}")
    contained = actual in {item.strip().lower() for item in literal}
    return contained if op == "in" else not contained


def _rules_at(container: dict) -> list[dict]:
    """skip_rules on a tree or a step, ignoring junk entries.

    Both placements exist and mean different things; build_tier1_plan applies
    them separately.
    """
    rules = container.get("skip_rules")
    if not isinstance(rules, list):
        return []
    return [rule for rule in rules if isinstance(rule, dict)]


def _tier_of(step: dict) -> int:
    """The step's tier, defaulting to 1 -- the agent name is the stronger signal,
    and defaulting the other way would silently drop the step."""
    try:
        return int(step.get("tier", 1))
    except (TypeError, ValueError):
        return 1


def build_tier1_plan(
    category: str,
    guidance_json: str | None,
    classification: dict | None = None,
) -> dict:
    """Decide which Tier 1 agents run and what each is told to focus on.

    `guidance_json` is get_triage_guidance's raw return, or None; any error or
    malformed shape triggers fail-open. Returns a JSON-serialisable plan:
        selected      list[str]  -- agents to run, tree order first
        skipped       list[dict] -- {agent, reason} for the rest
        focus         dict       -- agent -> the tree's `action` text
        tree_version  int | None
        tree_source   str        -- how the selection was reached
        fail_open     bool       -- True when the full 9 run by fallback
        notes         list[str]
    """
    classification = classification or {}
    category = (category or "unknown").strip() or "unknown"
    notes: list[str] = []

    default = DEFAULT_TIER1_SELECTION.get(category)
    if default is None:
        # No in-repo opinion for this category, so narrowing would be guessing.
        notes.append(
            f"category '{category}' has no in-repo Tier 1 selection; running all "
            f"{len(ALL_TIER1_AGENTS)} agents"
        )
        return _fail_open_plan(None, "unknown-category", notes)

    tree, tree_error = _parse_guidance(guidance_json)
    if tree_error:
        notes.append(f"{tree_error}; running all {len(ALL_TIER1_AGENTS)} agents")
        return _fail_open_plan(None, "tree-unavailable", notes)

    tree_version = tree.get("version") if isinstance(tree.get("version"), int) else None
    steps = tree.get("triage_steps")
    if not isinstance(steps, list) or not steps:
        notes.append(
            f"tree for '{category}' carries no usable triage_steps; running all "
            f"{len(ALL_TIER1_AGENTS)} agents"
        )
        return _fail_open_plan(tree_version, "tree-malformed", notes)

    # A tree-level rule covers the whole category, so it must also apply to
    # agents carried over from the in-repo list below, which no per-step pass
    # would reach. Step-level rules apply only to their own agent.
    category_rule = _first_matching_skip_rule(
        _rules_at(tree), classification, category, notes
    )

    tree_order: list[str] = []
    focus: dict[str, str] = {}
    skipped_by_rule: dict[str, str] = {}

    for step in sorted(
        (s for s in steps if isinstance(s, dict)),
        key=lambda s: _coerce_order(s.get("order")),
    ):
        agent = TREE_AGENT_TO_ADK_AGENT.get(str(step.get("agent", "")).strip())
        if agent is None or _tier_of(step) != 1:
            continue  # tier-2 step, historical_context step, or an unknown name

        rule_reason = category_rule or _first_matching_skip_rule(
            _rules_at(step), classification, category, notes
        )
        if rule_reason is not None:
            skipped_by_rule[agent] = rule_reason
            continue

        if agent not in tree_order:
            tree_order.append(agent)
        action = str(step.get("action", "")).strip()
        if action and agent not in focus:
            focus[agent] = action

    selected = [a for a in tree_order if a in default]
    if category_rule is None:
        # Carry over agents the tree omits: a missing step usually means a stale
        # tree, and dropping them would make that look like deliberate exclusion.
        selected += [a for a in default if a not in tree_order and a not in skipped_by_rule]
    else:
        # Record the reason against each so the plan and sentinels stay accurate.
        for agent in default:
            skipped_by_rule.setdefault(agent, category_rule)

    if not selected:
        # AC 8: empty means a broken tree, not "investigate nothing".
        notes.append(
            f"tree for '{category}' selected zero Tier 1 agents (an empty selection "
            f"is a bug, not a valid tree); running all {len(ALL_TIER1_AGENTS)} agents"
        )
        return _fail_open_plan(tree_version, "empty-selection", notes)

    skipped = [
        {
            "agent": agent,
            "reason": skipped_by_rule.get(
                agent, f"not in the Tier 1 selection for category '{category}'"
            ),
        }
        for agent in ALL_TIER1_AGENTS
        if agent not in selected
    ]

    return {
        "category": category,
        "selected": selected,
        "skipped": skipped,
        "focus": {a: focus[a] for a in selected if a in focus},
        "tree_version": tree_version,
        "tree_source": "tree",
        "fail_open": False,
        "notes": notes,
    }


def _coerce_order(value: Any) -> float:
    """Sort key for a step's `order`. Junk sorts last rather than raising."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _parse_guidance(guidance_json: str | None) -> tuple[dict, str | None]:
    """Turn get_triage_guidance's JSON string into a tree dict, or say why not."""
    if not guidance_json:
        return {}, "decision tree was not fetched"
    try:
        tree = json.loads(guidance_json)
    except (TypeError, ValueError) as exc:
        return {}, f"decision tree response was not JSON ({exc})"
    if not isinstance(tree, dict):
        return {}, "decision tree response was not a JSON object"
    if tree.get("error"):
        return {}, f"decision tree lookup failed ({tree['error']})"
    if "triage_steps" not in tree:
        # get_triage_guidance's "no document" branch returns a `guidance` message
        # and no triage_steps.
        return {}, str(tree.get("guidance") or "decision tree document does not exist")
    return tree, None


def _first_matching_skip_rule(
    rules: list[dict],
    classification: dict,
    category: str,
    notes: list[str],
) -> str | None:
    """Return the reason of the first firing skip rule, or None.

    An unparseable rule is recorded and IGNORED, never treated as firing. A rule
    nobody can read is a config defect; acting on it would silently drop an
    investigation agent on the strength of a string that does not mean anything.
    """
    fields = {
        "severity": classification.get("severity", ""),
        "environment": classification.get("environment", ""),
        "service_name": classification.get("service_name", ""),
        "category": classification.get("category", "") or category,
    }
    for rule in rules:
        condition = str(rule.get("condition", "")).strip()
        if not condition:
            continue
        try:
            fired = evaluate_skip_condition(condition, fields)
        except SkipRuleError as exc:
            notes.append(f"skip rule ignored ({exc})")
            continue
        if fired:
            reason = str(rule.get("reason", "")).strip() or "no reason recorded"
            return f"skip rule fired -- {condition} -- {reason}"
    return None


def _fail_open_plan(tree_version: int | None, source: str, notes: list[str]) -> dict:
    """AC 7. Every degraded path lands here: all 9 agents, and it says so."""
    return {
        "category": None,
        "selected": list(ALL_TIER1_AGENTS),
        "skipped": [],
        "focus": {},
        "tree_version": tree_version,
        "tree_source": source,
        "fail_open": True,
        "notes": notes,
    }


def skip_sentinel(agent: str, plan: dict) -> str:
    """Text for a skipped agent's `{findings_*}` key (AC 5).

    An unset key renders as "" and reads to synthesis as "checked, found
    nothing". This says the opposite in words neither prompt can mistake.
    """
    reason = next(
        (s["reason"] for s in plan.get("skipped", []) if s.get("agent") == agent),
        "not selected for this category",
    )
    version = plan.get("tree_version")
    provenance = (
        f"decision tree '{plan.get('category')}' v{version}"
        if version is not None
        else f"decision tree routing ({plan.get('tree_source')})"
    )
    return (
        f"NOT RUN -- skipped by {provenance}: {reason}. "
        "This is a ROUTING decision, not a tool failure and not an empty result. "
        "This source was NOT checked, so it is not evidence of absence: do not "
        "report it as healthy, as clear, or as returning no data, and do not "
        "count it as a cross-validation source."
    )


def routing_note(plan: dict) -> str:
    """One-line human summary of the routing decision (AC 6)."""
    selected = ", ".join(plan.get("selected", [])) or "none"
    skipped = plan.get("skipped", [])
    version = plan.get("tree_version")
    tree = (
        f"decision tree v{version}"
        if version is not None and not plan.get("fail_open")
        else f"FAIL-OPEN ({plan.get('tree_source')})"
    )
    parts = [f"Tier 1 routed by {tree}", f"ran: {selected}"]
    parts.append(
        "skipped: " + (", ".join(s["agent"] for s in skipped) if skipped else "none")
    )
    if plan.get("notes"):
        parts.append("notes: " + "; ".join(plan["notes"]))
    return " | ".join(parts)

"""Scores whether each Tier 1 agent contributed to an incident, and folds that
into the category's decision-tree hit rates. Pure functions; no I/O.

An agent CONTRIBUTED when a distinctive artifact it produced (backticked span,
URL, id-shaped token) appears in the synthesis or report and was not already in
the alert, service_context, or past_incidents.

Rules that look right and are not, measured 2026-08-20 over the 483 stored
incidents carrying findings:
  - "findings key is non-empty" scores 1.00 for all nine agents -- every key is
    non-blank in every document, so it is a constant, not a signal.
  - The agent's own `Confidence:` bullet parses in 26 of 483 findings_cloud_run
    fields (1 of 115 in 2026-08): it measures prompt drift, and it is
    self-assessment.
  - The verification score is per-run and cannot attribute to an agent, so it
    gates the incident instead (is_incident_usable).

Subtracting tokens the agent was handed is what makes the number mean anything:
on findings_teams it moves the rate 0.594 -> 0.120.

Tier 2 agents are never scored -- persistence writes tier2_findings as one flat
string with no per-agent split, so they keep the 0.5 prior at sample_count 0.

Blind spot: an agent that rules a hypothesis out without leaving a quotable
artifact scores as a miss. Hence the heavy prior, and why a human edit outranks
any number of samples.
"""

from __future__ import annotations

import re

# Explicit map: the key names are not a mechanical transform of the agent names
# (findings_deploy -> deploy_correlation_agent).
FINDINGS_KEY_TO_AGENT: dict[str, str] = {
    "findings_gcp_logging": "gcp_logging_agent",
    "findings_gcp_metrics": "gcp_metrics_agent",
    "findings_datadog": "datadog_agent",
    "findings_sentry": "sentry_agent",
    "findings_cloud_run": "cloud_run_health_agent",
    "findings_deploy": "deploy_correlation_agent",
    "findings_known_issues": "known_issues_agent",
    "findings_teams": "teams_context_agent",
    "findings_compute_engine": "compute_engine_health_agent",
}

# A skipped agent's findings key holds this prefix (tier1_routing.skip_sentinel).
# The key is PRESENT, so an absence check misses it, and the text carries no
# artifact, so it would score a miss for never having run -- which then argues
# for skipping it again.
SKIP_SENTINEL_PREFIX = "NOT RUN --"

# hit_rate = (PRIOR_WEIGHT*PRIOR + hits) / (PRIOR_WEIGHT + samples).
# PRIOR 0.5 matches what every tree already holds, so zero samples computes to
# today's value. PRIOR_WEIGHT 10 caps one incident's movement at 0.045.
PRIOR = 0.5
PRIOR_WEIGHT = 10.0

# Redundant given the prior, but keeps a step visible to any consumer that
# filters on hit_rate > 0.
HIT_RATE_FLOOR = 0.05
HIT_RATE_CEILING = 0.95

# A token emitted by more than this many agents in one run is ambient context,
# not any one agent's find. Two or three converging is corroboration, which
# INVESTIGATION_RULES #2 requires. Softest constant here; worth 2-5 points.
AMBIENT_MAX = 3

# Runs whose conclusion is untrustworthy: tracing whose evidence reached it
# measures the path to a wrong answer. WEAK is kept -- excluding it too would
# drop another 60 documents for little gain.
UNUSABLE_DIAGNOSIS = frozenset({"UNSUPPORTED"})
UNUSABLE_TIER = frozenset({"UNRELIABLE"})

# >= 6 chars because `id`, `200`, `true` are noise.
_BACKTICK = re.compile(r"`([^`\n]{3,120})`")
_URL = re.compile(r"https?://[^\s)\]<>\"']+")
# Deliberately narrow: a broad id regex matches dates and ports.
_IDLIKE = re.compile(r"\b(?:[A-Z][A-Z0-9]{2,}-[A-Z0-9-]{3,}|\d{6,}(?:-rc)?|ServerTasks-\d+)\b")
# snake_case is a tool name the agent is narrating, not evidence it retrieved.
_TOOLNAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:\(\))?$")
_STOPWORDS = frozenset({"true", "false", "none", "null", "n/a", "unknown", "string", "number"})


def artifact_tokens(text: str) -> set[str]:
    """Backticked spans, URLs and id-shaped tokens in `text`.

    Prose is excluded: two agents describing the same outage in English is not
    evidence of reuse, a verbatim revision id is.
    """
    if not isinstance(text, str) or not text:
        return set()

    out: set[str] = set()
    for match in _BACKTICK.finditer(text):
        token = match.group(1).strip()
        if len(token) < 6 or token.lower() in _STOPWORDS or _TOOLNAME.match(token):
            continue
        out.add(token)
    for match in _URL.finditer(text):
        out.add(match.group(0).rstrip(".,;"))
    for match in _IDLIKE.finditer(text):
        out.add(match.group(0))
    return out


def is_incident_usable(incident: dict, require_verified: bool = False) -> tuple[bool, str]:
    """Should this stored incident inform hit rates? Returns (usable, reason).

    The reason string is bucketed and printed by the miner, so each cause must
    stay distinguishable.

    `require_verified` also drops runs with no readable score (76 of 483, from
    the ADO-200015 parser defect). Off by default: enabling it biases the corpus
    toward post-fix runs, which are also post-prompt-change runs.
    """
    findings = incident.get("findings")
    if not isinstance(findings, dict) or not findings:
        return False, "no findings map"

    downstream = (incident.get("report") or "") + (incident.get("synthesis") or "")
    if not isinstance(downstream, str) or not downstream.strip():
        # Nothing for evidence to be reused BY, so every agent would score a
        # miss for a reason unrelated to the agents.
        return False, "no synthesis or report to measure reuse against"

    verification = incident.get("verification")
    if not isinstance(verification, dict) or not verification:
        if require_verified:
            return False, "no verification score"
        return True, "unverified (counted)"

    diagnosis = str(verification.get("diagnosis") or "").strip().upper()
    tier = str(verification.get("automated_tier") or verification.get("combined_tier") or "").strip().upper()
    if diagnosis in UNUSABLE_DIAGNOSIS:
        return False, f"diagnosis={diagnosis}"
    if tier in UNUSABLE_TIER:
        return False, f"automated_tier={tier}"
    if require_verified and not diagnosis and not tier:
        return False, "verification present but carries no verdict"
    return True, f"diagnosis={diagnosis or '?'} tier={tier or '?'}"


def score_contributions(incident: dict) -> dict[str, bool]:
    """Per-agent contributed / did-not-contribute for one stored incident.

    Keyed by agent name so the result drops onto `triage_steps[].agent`. An
    agent that did not run is omitted, not scored a miss: never running is not
    the same as running and finding nothing. That covers an absent key and a
    skip sentinel, which is present but means the same thing.
    """
    findings = incident.get("findings")
    if not isinstance(findings, dict):
        return {}

    downstream = artifact_tokens(
        (incident.get("report") or "") + "\n" + (incident.get("synthesis") or "")
    )
    handed_in = artifact_tokens(
        ((incident.get("alert_info") or {}).get("raw_text") or "")
        + "\n"
        + (incident.get("service_context") or "")
        + "\n"
        + (incident.get("past_incidents") or "")
    )

    # Novel tokens per agent, and how many agents emitted each one.
    per_agent: dict[str, set[str]] = {}
    emitters: dict[str, int] = {}
    for key, agent in FINDINGS_KEY_TO_AGENT.items():
        value = findings.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if value.lstrip().startswith(SKIP_SENTINEL_PREFIX):
            continue
        novel = artifact_tokens(value) - handed_in
        per_agent[agent] = novel
        for token in novel:
            emitters[token] = emitters.get(token, 0) + 1

    return {
        agent: any(
            token in downstream and emitters[token] <= AMBIENT_MAX for token in novel
        )
        for agent, novel in per_agent.items()
    }


def posterior_hit_rate(hits: int, samples: int) -> float:
    """Prior-anchored hit rate, clamped to [HIT_RATE_FLOOR, HIT_RATE_CEILING].

    A Beta posterior rather than an EWMA: it carries its own sample size, so 1
    sample and 200 do not produce equally authoritative-looking numbers, and it
    cannot reach 0 or 1 at any count.
    """
    samples = max(int(samples), 0)
    hits = min(max(int(hits), 0), samples)
    raw = (PRIOR_WEIGHT * PRIOR + hits) / (PRIOR_WEIGHT + samples)
    return round(min(max(raw, HIT_RATE_FLOOR), HIT_RATE_CEILING), 4)


def apply_contributions(
    tree: dict,
    contributions_by_agent: dict[str, tuple[int, int]],
    incidents_analyzed: int,
) -> tuple[dict, list[str]]:
    """Fold (hits, samples) per agent into a tree. Returns (new_tree, changes).

    Does not mutate `tree`, and writes no timestamp, version or log entry -- the
    caller owns those, since only it knows whether the write landed.

    Locks:
      - `optimization_pinned` on the tree: nothing changes.
      - `human_locked` on a step: counters still accumulate so a reviewer can
        see what the automatic path would have said, but hit_rate is untouched.

    An agent listed more than once is skipped entirely. The signal is per agent,
    not per step, so content-archive (deploy_correlation_agent five times, hand-set
    0.9/0.8/0.7/0.85/0.8) would be flattened to one value.
    """
    new_tree = dict(tree)
    if new_tree.get("optimization_pinned"):
        return new_tree, []

    steps = [dict(s) for s in (new_tree.get("triage_steps") or [])]
    changes: list[str] = []

    seen: dict[str, int] = {}
    for step in steps:
        seen[step.get("agent")] = seen.get(step.get("agent"), 0) + 1
    ambiguous = {agent for agent, count in seen.items() if count > 1}
    for agent in sorted(a for a in ambiguous if a in contributions_by_agent):
        changes.append(
            f"{agent}: SKIPPED, appears {seen[agent]} times in triage_steps -- the "
            f"contribution signal is per agent and cannot attribute to one step"
        )

    for step in steps:
        agent = step.get("agent")
        if agent in ambiguous:
            continue
        observed = contributions_by_agent.get(agent)
        if not observed:
            # Tier 2 lands here every pass -- tier2_findings is one flat string.
            continue

        new_hits, new_samples = observed
        total_hits = int(step.get("hit_count") or 0) + int(new_hits)
        total_samples = int(step.get("sample_count") or 0) + int(new_samples)
        step["hit_count"] = total_hits
        step["sample_count"] = total_samples

        if step.get("human_locked"):
            changes.append(
                f"{agent}: {new_hits}/{new_samples} observed, hit_rate held at "
                f"{step.get('hit_rate')} (human_locked "
                f"{step.get('human_locked_by') or 'unknown'})"
            )
            continue

        before = step.get("hit_rate")
        after = posterior_hit_rate(total_hits, total_samples)
        step["hit_rate"] = after
        if before != after:
            changes.append(
                f"{agent}: hit_rate {before} -> {after} "
                f"({total_hits}/{total_samples} contributed)"
            )

    new_tree["triage_steps"] = steps
    new_tree["incident_count"] = int(new_tree.get("incident_count") or 0) + int(incidents_analyzed)
    return new_tree, changes


def evaluate_skip_rule(rule: dict, context: dict) -> bool:
    """True when a structured skip rule matches. Consumed by ADO-200023 routing.

    Operands, not an expression: {"field": ..., "op": ..., "value": ...}. The
    design in plan-memory-store-integration.md:335 stored Python source and
    eval()d it, making any Firestore write arbitrary code execution here.

    A malformed rule returns False and so runs the step. Failing open costs
    tokens; failing closed silently skips evidence gathering.
    """
    if not isinstance(rule, dict):
        return False
    field = rule.get("field")
    op = rule.get("op")
    if not isinstance(field, str) or field not in context:
        return False
    actual = context.get(field)
    expected = rule.get("value")
    if op == "==":
        return str(actual).lower() == str(expected).lower()
    if op == "!=":
        return str(actual).lower() != str(expected).lower()
    if op == "in":
        return isinstance(expected, (list, tuple)) and str(actual).lower() in {
            str(v).lower() for v in expected
        }
    if op == "not_in":
        return isinstance(expected, (list, tuple)) and str(actual).lower() not in {
            str(v).lower() for v in expected
        }
    return False

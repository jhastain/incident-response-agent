"""Firestore tools for incident memory and decision tree operations."""

import functools
import json
import os
import re
from datetime import UTC, datetime

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore

from .decision_tree_learning import (
    HIT_RATE_CEILING,
    HIT_RATE_FLOOR,
    apply_contributions,
    is_incident_usable,
    score_contributions,
)


@functools.cache
def _get_db() -> firestore.Client:
    """Lazily create the Firestore client for the current environment's project.

    Created on first use, not at import time. Module-level client construction
    ran google.auth.default() during `import app`, which
    blocks the container's app-load path (fast_api_app importing the agent) and
    can stall cold starts on the self-hosted 1.1.0 runtime. Deferring it keeps
    import side-effect-free.

    The project comes from GOOGLE_CLOUD_PROJECT with no default: each env owns a
    `(default)` database and incident history is deliberately not shared, so a
    missing env var must fail loudly rather than write Test data into Prod.
    """
    return firestore.Client(project=os.environ["GOOGLE_CLOUD_PROJECT"])

TIER1_AGENTS = [
    "gcp_logging_agent",
    "gcp_metrics_agent",
    "datadog_agent",
    "sentry_agent",
    "cloud_run_health_agent",
    "deploy_correlation_agent",
    "known_issues_agent",
    "teams_context_agent",
    "compute_engine_health_agent",
]

# `**Root Cause (STRONG EVIDENCE):**` / `**Root cause:** ...` / `## Root Cause`.
# The confidence parenthetical and the trailing colon are both optional because the
# report template has carried all three shapes.
_ROOT_CAUSE_HEADING = re.compile(
    r"^\s*(?:\*\*|#{1,4}\s*)root\s+cause\b[^\n]*", re.IGNORECASE | re.MULTILINE
)
# A bullet whose label names the mechanism. Historical reports lead their Root Cause
# block with `- Mechanism: ...`; since ADO-200018 the same bullet leads Key Findings
# instead. Either way it is the one-line causal claim we want, in preference to the
# Trigger/Signal/Change bullets that used to follow it.
_MECHANISM_BULLET = re.compile(r"^\s*[-*]\s*\*{0,2}mechanism\*{0,2}\s*:\s*(.+)", re.IGNORECASE)


def _root_cause_of(data: dict) -> str:
    """Best-effort root cause for a stored incident.

    `find_similar_incidents` originally read `root_cause_analysis.summary`, a field
    `persistence_agent` has never written — verified against live Firestore, where no
    agent-authored incident carries that key. Every result therefore came back with
    `root_cause: ""`, so historical context handed synthesis a list of incident ids
    with no causes attached, and the agent treated each alert as the first of its kind.

    Falls back to parsing the stored report, which is where the cause actually lives.
    Returns "" when nothing is recoverable — an empty string is honest here, and the
    caller already distinguishes it from a real cause.
    """
    legacy = (data.get("root_cause_analysis") or {}).get("summary", "")
    if legacy:
        return legacy.strip()

    report = data.get("report") or ""
    if not report:
        return ""

    match = _ROOT_CAUSE_HEADING.search(report)
    if not match:
        # ADO-200018 cut the Root Cause section from the report template; the causal
        # claim now leads Key Findings as "- Mechanism: ...". Old stored incidents
        # still carry the heading, so the heading path above stays first.
        for line in report.splitlines():
            mech = _MECHANISM_BULLET.match(line)
            if mech:
                return mech.group(1).strip()
        return ""

    # Inline form: the cause sits on the heading line itself.
    inline = report[match.start() : match.end()]
    after_colon = inline.split(":", 1)[1].strip().strip("*").strip() if ":" in inline else ""
    if after_colon:
        return after_colon

    # Block form: take the Mechanism bullet if present, else the first bullet.
    body = report[match.end() :]
    first_bullet = ""
    for line in body.splitlines():
        if not line.strip():
            if first_bullet:
                break
            continue
        mech = _MECHANISM_BULLET.match(line)
        if mech:
            return mech.group(1).strip()
        if line.lstrip().startswith(("-", "*")) and not first_bullet:
            first_bullet = line.lstrip().lstrip("-*").strip()
        elif first_bullet:
            break
    return first_bullet


def write_incident_to_firestore(incident_id: str, incident_json: str) -> str:
    """Write an incident document to the incidents collection.

    Args:
        incident_id: The unique incident identifier.
        incident_json: The full incident JSON string containing all incident data.

    Returns:
        JSON string confirming the write or reporting an error.
    """
    try:
        incident_data = json.loads(incident_json)
        incident_data["created_at"] = firestore.SERVER_TIMESTAMP
        incident_data["updated_at"] = firestore.SERVER_TIMESTAMP

        doc_ref = _get_db().collection("incidents").document(incident_id)
        doc_ref.set(incident_data)

        return json.dumps({
            "status": "success",
            "incident_id": incident_id,
            "collection": "incidents",
        })
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in incident_json: {e}"})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"write_incident_to_firestore failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"write_incident_to_firestore unexpected error: {type(e).__name__}: {e}"})


def find_similar_incidents(category: str, service: str = "", limit: int = 5) -> str:
    """Find similar past incidents by category, with optional service filter.

    Combines verified incidents (HIGH/MEDIUM confidence) with human-corrected incidents.

    Args:
        category: The incident category to search for.
        service: Optional service name to filter results (applied in-memory after query).
        limit: Maximum number of verified incidents to return (default 5, corrected always limited to 3).

    Returns:
        JSON string containing an array of similar incidents with source, root_cause, and confidence info.
    """
    try:
        results = []

        verified_query = (
            _get_db().collection("incidents")
            .where("category", "==", category)
            .where("verification.combined_tier", "in", ["HIGH", "MEDIUM"])
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit * 2)
        )
        verified_docs = verified_query.stream()

        for doc in verified_docs:
            data = doc.to_dict()
            if service and data.get("service", "") != service:
                continue

            results.append({
                "incident_id": doc.id,
                "source": "verified",
                "root_cause": _root_cause_of(data),
                "confidence_tier": data.get("verification", {}).get("combined_tier", "UNKNOWN"),
            })

            if len([r for r in results if r["source"] == "verified"]) >= limit:
                break

        corrected_query = (
            _get_db().collection("incidents")
            .where("verification.corrected", "==", True)
            .where("verification.corrected_category", "==", category)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(3)
        )
        corrected_docs = corrected_query.stream()

        for doc in corrected_docs:
            data = doc.to_dict()
            if service and data.get("service", "") != service:
                continue

            if doc.id not in [r["incident_id"] for r in results]:
                results.append({
                    "incident_id": doc.id,
                    "source": "human_corrected",
                    # A human correction outranks anything parsed from the agent's own
                    # report — that is the whole point of the correction — so it is
                    # only the fallback that changes here, not the precedence.
                    "root_cause": (
                        data.get("verification", {}).get("corrected_root_cause", "")
                        or _root_cause_of(data)
                    ),
                    "correction_note": data.get("human_feedback", {}).get("correction_note", ""),
                    "solution_type": data.get("human_feedback", {}).get("solution_type", ""),
                })

        return json.dumps({"count": len(results), "incidents": results})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"find_similar_incidents failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"find_similar_incidents unexpected error: {type(e).__name__}: {e}"})


def get_triage_guidance(category: str) -> str:
    """Get learned triage guidance for a specific category from decision trees.

    Args:
        category: The incident category to look up.

    Returns:
        JSON string containing formatted triage steps with order, agent, action, and hit_rate,
        or a message if no guidance exists yet.
    """
    try:
        doc_ref = _get_db().collection("decision_trees").document(category)
        doc = doc_ref.get()

        if not doc.exists:
            return json.dumps({
                "category": category,
                "guidance": f"No learned triage guidance for category '{category}' yet.",
            })

        data = doc.to_dict()
        triage_steps = data.get("triage_steps", [])

        return json.dumps({
            "category": category,
            "version": data.get("version", 1),
            "incident_count": data.get("incident_count", 0),
            "last_optimized": data.get("last_optimized"),
            "triage_steps": triage_steps,
        })
    except GoogleAPICallError as e:
        return json.dumps({"error": f"get_triage_guidance failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"get_triage_guidance unexpected error: {type(e).__name__}: {e}"})


def update_verification_scores(incident_id: str, verification_json: str) -> str:
    """Update the verification subdocument for an existing incident.

    Args:
        incident_id: The incident document ID to update.
        verification_json: JSON string containing verification data.

    Returns:
        JSON string confirming the update or reporting an error.
    """
    try:
        verification_data = json.loads(verification_json)

        doc_ref = _get_db().collection("incidents").document(incident_id)
        doc_ref.update({
            "verification": verification_data,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })

        return json.dumps({
            "status": "success",
            "incident_id": incident_id,
            "field": "verification",
        })
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in verification_json: {e}"})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"update_verification_scores failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"update_verification_scores unexpected error: {type(e).__name__}: {e}"})


def submit_human_feedback(incident_id: str, feedback_json: str) -> str:
    """Submit human feedback for an incident and recalculate combined confidence tier.

    Args:
        incident_id: The incident document ID to update.
        feedback_json: JSON string containing human feedback data with F1_category_accuracy field.

    Returns:
        JSON string confirming the update with new combined_tier or reporting an error.
    """
    try:
        feedback_data = json.loads(feedback_json)
        f1_accuracy = feedback_data.get("root_cause_accuracy", "")

        doc_ref = _get_db().collection("incidents").document(incident_id)
        doc = doc_ref.get()

        if not doc.exists:
            return json.dumps({"error": f"Incident {incident_id} not found"})

        incident_data = doc.to_dict()
        verification = incident_data.get("verification", {})
        automated_tier = verification.get("automated_tier", "LOW")

        if f1_accuracy == "WRONG":
            combined_tier = "LOW"
            corrected = True
            corrected_root_cause = feedback_data.get("corrected_root_cause", "")
            corrected_category = feedback_data.get("corrected_category", "")

            doc_ref.update({
                "human_feedback": feedback_data,
                "verification.combined_tier": combined_tier,
                "verification.corrected": corrected,
                "verification.corrected_root_cause": corrected_root_cause,
                "verification.corrected_category": corrected_category,
                "verification.tier_updated_at": datetime.now(UTC).isoformat(),
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        elif f1_accuracy == "CORRECT":
            combined_tier = "HIGH" if automated_tier in ["HIGH", "MEDIUM"] else automated_tier

            doc_ref.update({
                "human_feedback": feedback_data,
                "verification.combined_tier": combined_tier,
                "verification.tier_updated_at": datetime.now(UTC).isoformat(),
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        elif f1_accuracy == "PARTIALLY_CORRECT":
            combined_tier = min([automated_tier, "MEDIUM"], key=lambda x: ["LOW", "MEDIUM", "HIGH"].index(x))

            doc_ref.update({
                "human_feedback": feedback_data,
                "verification.combined_tier": combined_tier,
                "verification.tier_updated_at": datetime.now(UTC).isoformat(),
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        elif f1_accuracy == "INCONCLUSIVE":
            combined_tier = automated_tier

            doc_ref.update({
                "human_feedback": feedback_data,
                "verification.tier_updated_at": datetime.now(UTC).isoformat(),
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        else:
            return json.dumps({"error": f"Invalid F1_category_accuracy value: {f1_accuracy}"})

        return json.dumps({
            "status": "success",
            "incident_id": incident_id,
            "f1_accuracy": f1_accuracy,
            "combined_tier": combined_tier,
        })
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in feedback_json: {e}"})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"submit_human_feedback failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"submit_human_feedback unexpected error: {type(e).__name__}: {e}"})


def seed_decision_trees() -> str:
    """Seed the decision_trees collection with initial triage data from seed file.

    Loads decision tree seeds from ../data/decision_tree_seeds.json and writes them
    to Firestore. Skips categories that already exist.

    Returns:
        JSON string with count of seeded documents and any errors.
    """
    try:
        from pathlib import Path

        seed_file = Path(__file__).parent.parent / "data" / "decision_tree_seeds.json"

        if not seed_file.exists():
            return json.dumps({
                "error": f"Seed file not found at {seed_file}",
                "status": "failed",
            })

        with open(seed_file) as f:
            seed_data = json.load(f)

        seeded_count = 0
        skipped_count = 0

        for seed_doc in seed_data:
            category = seed_doc["category"]
            doc_ref = _get_db().collection("decision_trees").document(category)

            if doc_ref.get().exists:
                skipped_count += 1
                continue

            doc_ref.create(seed_doc)
            seeded_count += 1

        return json.dumps({
            "status": "success",
            "seeded_count": seeded_count,
            "skipped_count": skipped_count,
            "total_categories": len(seed_data),
        })
    except FileNotFoundError as e:
        return json.dumps({"error": f"Seed file not found: {e}"})
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in seed file: {e}"})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"seed_decision_trees failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"seed_decision_trees unexpected error: {type(e).__name__}: {e}"})


# ADO-200024: decision-tree feedback loop. The writers below are deliberately
# asymmetric -- the automatic path only moves hit rates, while the human path
# reorders, edits actions, and can lock a step or pin a tree against it.

# Both caps exist because a Firestore document is limited to ~1 MiB: an
# unbounded list eventually fails the write and takes the hit-rate update with
# it. Oldest entries fall off the front.
_APPLIED_IDS_CAP = 500
_OPTIMIZATION_LOG_CAP = 100


def _log_entry(version: int, actor: str, changes, incidents_analyzed: int, note: str = "") -> dict:
    """One `optimization_log` row. `actor` distinguishes automatic from human."""
    return {
        "version": version,
        "timestamp": datetime.now(UTC).isoformat(),
        "actor": actor,
        "changes": list(changes) if isinstance(changes, (list, tuple)) else [str(changes)],
        "incidents_analyzed": int(incidents_analyzed),
        "note": note,
    }


def _as_list(value) -> list[dict]:
    """Accept one dict or a list of them -- the CLI passes either."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return [v for v in value if isinstance(v, dict)]


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def record_incident_contributions(category: str, incident_json: str) -> str:
    """Fold one finished incident's per-agent contributions into its category tree.

    Idempotent by incident id: persistence retries on transient write failures
    and an Agent Engine session can be replayed, so `applied_incident_ids` makes
    a replay a no-op instead of a double count.

    Swallows its own failures into {"error": ...} like the rest of this module;
    persistence must never fail the run.
    """
    try:
        incident = json.loads(incident_json) if isinstance(incident_json, str) else incident_json
        if not isinstance(incident, dict):
            return json.dumps({"error": "incident_json did not parse to an object"})

        incident_id = incident.get("incident_id")
        if not incident_id:
            return json.dumps({"error": "incident has no incident_id; cannot deduplicate"})

        usable, reason = is_incident_usable(incident)
        if not usable:
            # Not an error -- an UNSUPPORTED run just must not teach the tree.
            return json.dumps({
                "status": "skipped",
                "incident_id": incident_id,
                "category": category,
                "reason": reason,
            })

        contributions = score_contributions(incident)
        if not contributions:
            return json.dumps({
                "status": "skipped",
                "incident_id": incident_id,
                "category": category,
                "reason": "no tier-1 findings to attribute",
            })

        db = _get_db()
        tree_ref = db.collection("decision_trees").document(category)
        transaction = db.transaction()

        @firestore.transactional
        def _apply(txn):
            snapshot = tree_ref.get(transaction=txn)
            if not snapshot.exists:
                # seed_decision_trees owns creation. A tree built from one
                # incident would have steps invented from whichever agents ran.
                return {"status": "skipped", "reason": f"no decision tree for '{category}'"}

            tree = snapshot.to_dict() or {}
            applied = list(tree.get("applied_incident_ids") or [])
            if incident_id in applied:
                return {"status": "already_applied", "reason": "incident already counted"}

            per_agent = {agent: (1 if hit else 0, 1) for agent, hit in contributions.items()}
            new_tree, changes = apply_contributions(tree, per_agent, incidents_analyzed=1)

            if tree.get("optimization_pinned"):
                # Still record the id, so unpinning later does not replay
                # history that already happened.
                txn.update(tree_ref, {
                    "applied_incident_ids": (applied + [incident_id])[-_APPLIED_IDS_CAP:],
                })
                return {"status": "pinned", "reason": "optimization_pinned; hit rates unchanged"}

            version = int(tree.get("version") or 1) + 1
            log = list(tree.get("optimization_log") or [])
            log.append(_log_entry(
                version=version,
                actor="automatic",
                changes=changes or ["no hit_rate movement"],
                incidents_analyzed=1,
                note=f"incident {incident_id} ({reason})",
            ))

            txn.update(tree_ref, {
                "triage_steps": new_tree["triage_steps"],
                "incident_count": new_tree["incident_count"],
                "version": version,
                "last_optimized": datetime.now(UTC).isoformat(),
                "optimization_log": log[-_OPTIMIZATION_LOG_CAP:],
                "applied_incident_ids": (applied + [incident_id])[-_APPLIED_IDS_CAP:],
            })
            return {"status": "success", "version": version, "changes": changes}

        result = _apply(transaction)
        return json.dumps({
            "incident_id": incident_id,
            "category": category,
            "contributed": sorted(a for a, hit in contributions.items() if hit),
            "did_not_contribute": sorted(a for a, hit in contributions.items() if not hit),
            **result,
        })
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in incident_json: {e}"})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"record_incident_contributions failed: {e}"})
    except Exception as e:
        return json.dumps({
            "error": f"record_incident_contributions unexpected error: {type(e).__name__}: {e}"
        })


def record_backfilled_hit_rates(
    category: str, tallies_json: str, incidents_analyzed: int, actor: str
) -> str:
    """Write corpus-mined (hits, samples) for one category. Used only by
    scripts/mine_decision_tree_hit_rates.py --apply.

    Absorbs many incidents at once and so cannot deduplicate them: this does NOT
    touch applied_incident_ids, and the live path will recount any of the same
    incidents later. The log entry stamps `backfill: true` so that is visible.
    Run it once, before enabling the live path for a category.

    Not exported from app.tools and not an agent tool -- nothing in the pipeline
    should be able to bulk-rewrite a tree.
    """
    try:
        tallies = json.loads(tallies_json) if isinstance(tallies_json, str) else tallies_json
        if not isinstance(tallies, dict) or not tallies:
            return json.dumps({"error": "tallies_json did not parse to a non-empty object"})
        if not str(actor or "").strip():
            return json.dumps({"error": "backfill requires an actor"})

        per_agent = {
            agent: (int(pair[0]), int(pair[1]))
            for agent, pair in tallies.items()
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        }

        db = _get_db()
        tree_ref = db.collection("decision_trees").document(category)
        transaction = db.transaction()

        @firestore.transactional
        def _apply(txn):
            snapshot = tree_ref.get(transaction=txn)
            if not snapshot.exists:
                return {"error": f"no decision tree for '{category}'"}
            tree = snapshot.to_dict() or {}
            if tree.get("optimization_pinned"):
                return {"status": "pinned", "reason": "optimization_pinned; nothing written"}

            new_tree, changes = apply_contributions(tree, per_agent, int(incidents_analyzed))
            if not changes:
                return {"status": "no_op", "reason": "no hit_rate movement"}

            version = int(tree.get("version") or 1) + 1
            log = list(tree.get("optimization_log") or [])
            entry = _log_entry(
                version=version,
                actor=actor,
                changes=changes,
                incidents_analyzed=int(incidents_analyzed),
                note=f"corpus backfill of {category} (ADO-200024)",
            )
            entry["backfill"] = True
            log.append(entry)

            txn.update(tree_ref, {
                "triage_steps": new_tree["triage_steps"],
                "incident_count": new_tree["incident_count"],
                "version": version,
                "last_optimized": datetime.now(UTC).isoformat(),
                "optimization_log": log[-_OPTIMIZATION_LOG_CAP:],
            })
            return {"status": "success", "version": version, "changes": changes}

        return json.dumps({"category": category, **_apply(transaction)})
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in tallies_json: {e}"})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"record_backfilled_hit_rates failed: {e}"})
    except Exception as e:
        return json.dumps({
            "error": f"record_backfilled_hit_rates unexpected error: {type(e).__name__}: {e}"
        })


def apply_engineer_correction(category: str, correction_json: str) -> str:
    """Apply an SRE's hand correction to a category's decision tree.

    `correction_json` needs `actor` and `reason`, plus any of:

      reorder:      ["agent", ...] -- unlisted agents keep their relative order
                    after the listed ones, so a partial list is valid.
      set_action:   {"agent": ..., "action": ...}  (one dict or a list)
      set_hit_rate: {"agent": ..., "hit_rate": 0.2} -- also sets human_locked.
      unlock:       ["agent", ...] -- back to the automatic path.
      add_skip_rule / remove_skip_rule: operands, never expression strings
                    (see evaluate_skip_rule).
      pinned:       freeze or unfreeze the whole tree.
    """
    try:
        correction = (
            json.loads(correction_json) if isinstance(correction_json, str) else correction_json
        )
        if not isinstance(correction, dict):
            return json.dumps({"error": "correction_json did not parse to an object"})

        actor = str(correction.get("actor") or "").strip()
        reason = str(correction.get("reason") or "").strip()
        if not actor or not reason:
            # Without these, nobody can later tell whether a hit_rate was
            # measured or asserted.
            return json.dumps({"error": "correction requires both 'actor' and 'reason'"})

        db = _get_db()
        tree_ref = db.collection("decision_trees").document(category)
        transaction = db.transaction()

        @firestore.transactional
        def _apply(txn):
            snapshot = tree_ref.get(transaction=txn)
            if not snapshot.exists:
                return {"error": f"no decision tree for '{category}'"}

            tree = snapshot.to_dict() or {}
            steps = [dict(s) for s in (tree.get("triage_steps") or [])]
            by_agent = {s.get("agent"): s for s in steps}
            changes: list[str] = []

            for item in _as_list(correction.get("set_action")):
                step = by_agent.get(item.get("agent"))
                if step is None:
                    changes.append(f"set_action skipped: no step for {item.get('agent')}")
                    continue
                before = step.get("action")
                step["action"] = str(item.get("action") or "")
                changes.append(f"{step['agent']}: action {before!r} -> {step['action']!r}")

            for item in _as_list(correction.get("set_hit_rate")):
                step = by_agent.get(item.get("agent"))
                if step is None:
                    changes.append(f"set_hit_rate skipped: no step for {item.get('agent')}")
                    continue
                # Clamped like a learned value: a literal 0 lets a consumer
                # filtering on hit_rate drop the step, leaving no way back.
                raw = float(item.get("hit_rate"))
                value = round(min(max(raw, HIT_RATE_FLOOR), HIT_RATE_CEILING), 4)
                before = step.get("hit_rate")
                step["hit_rate"] = value
                step["human_locked"] = True
                step["human_locked_by"] = actor
                step["human_locked_at"] = datetime.now(UTC).isoformat()
                step["human_locked_reason"] = reason
                note = "" if value == raw else f" (clamped from {raw})"
                changes.append(
                    f"{step['agent']}: hit_rate {before} -> {value}{note}, human_locked by {actor}"
                )

            for agent in _as_str_list(correction.get("unlock")):
                step = by_agent.get(agent)
                if step is None:
                    continue
                for field in ("human_locked", "human_locked_by", "human_locked_at",
                              "human_locked_reason"):
                    step.pop(field, None)
                changes.append(f"{agent}: unlocked, automatic path resumes")

            order = _as_str_list(correction.get("reorder"))
            if order:
                rank = {agent: i for i, agent in enumerate(order)}
                # Unnamed agents keep their relative order, after the named
                # ones. Index captured up front: steps.index inside the key
                # would shift as the list sorts.
                current = {id(s): i for i, s in enumerate(steps)}
                steps.sort(key=lambda s: rank.get(s.get("agent"), len(order) + current[id(s)]))
                changes.append("reordered: " + ", ".join(s.get("agent", "?") for s in steps))

            rules = list(tree.get("skip_rules") or [])
            for rule in _as_list(correction.get("add_skip_rule")):
                if not rule.get("field") or not rule.get("op"):
                    changes.append(f"add_skip_rule rejected (needs field and op): {rule}")
                    continue
                rules.append({
                    **rule,
                    "added_by": actor,
                    "added_at": datetime.now(UTC).isoformat(),
                })
                changes.append(
                    f"skip_rule added: {rule.get('agent') or 'category'} when "
                    f"{rule['field']} {rule['op']} {rule.get('value')}"
                )
            for rule in _as_list(correction.get("remove_skip_rule")):
                keep = [
                    r for r in rules
                    if not (r.get("field") == rule.get("field")
                            and r.get("op") == rule.get("op")
                            and r.get("value") == rule.get("value")
                            and r.get("agent") == rule.get("agent"))
                ]
                if len(keep) != len(rules):
                    changes.append(f"skip_rule removed: {rule}")
                rules = keep

            # Consumers read `order`, not list position; leaving the two
            # disagreeing after a reorder is a half-applied edit.
            for i, step in enumerate(steps, start=1):
                step["order"] = i

            update: dict = {"triage_steps": steps, "skip_rules": rules}

            if "pinned" in correction:
                pinned = bool(correction["pinned"])
                update["optimization_pinned"] = pinned
                changes.append(
                    f"optimization_pinned -> {pinned}"
                    + (" (automatic hit-rate updates stopped)" if pinned
                       else " (automatic updates resume)")
                )

            if not changes:
                return {"status": "no_op", "reason": "correction contained no applicable edits"}

            version = int(tree.get("version") or 1) + 1
            log = list(tree.get("optimization_log") or [])
            log.append(_log_entry(
                version=version,
                actor=actor,
                changes=changes,
                incidents_analyzed=0,
                note=reason,
            ))
            update["version"] = version
            update["last_optimized"] = datetime.now(UTC).isoformat()
            update["optimization_log"] = log[-_OPTIMIZATION_LOG_CAP:]

            txn.update(tree_ref, update)
            return {"status": "success", "version": version, "changes": changes}

        return json.dumps({"category": category, "actor": actor, **_apply(transaction)})
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in correction_json: {e}"})
    except (TypeError, ValueError) as e:
        return json.dumps({"error": f"Invalid correction payload: {type(e).__name__}: {e}"})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"apply_engineer_correction failed: {e}"})
    except Exception as e:
        return json.dumps({
            "error": f"apply_engineer_correction unexpected error: {type(e).__name__}: {e}"
        })

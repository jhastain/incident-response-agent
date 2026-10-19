"""Ship verification_agent's scores to Datadog as evaluations.

verification_agent already scores diagnosis / efficiency and counts valid vs
invalid evidence citations on EVERY run, working from raw tool output to avoid
anchoring on the report's own confidence. That is a better correctness signal
than any generic template, and it is already paid for — this bridge costs zero
additional tokens.

Scores attach at eval_scope="trace" because they describe the whole
investigation, not one span.

ADO-200019 dropped the `solution` dimension, so `ir_solution_actionability`
stops being emitted from this build forward. The label is NOT renamed or
reused: its historical points stay queryable in Datadog and simply end at the
deploy. Any monitor or dashboard widget keyed on it will go no-data rather than
wrong — which is the honest outcome, since the dimension it measured (is the
proposed fix actionable?) no longer exists in the report.
"""

from __future__ import annotations

import logging

from ..app_utils.dd_observability import is_enabled, resolved_ml_app

logger = logging.getLogger(__name__)

# label -> (state key, notes key in verification["reasoning"])
# `solution` was removed here by ADO-200019 — see the module docstring.
_CATEGORICAL = (
    ("ir_diagnosis_quality", "diagnosis", "diagnosis_notes"),
    ("ir_investigation_efficiency", "efficiency", "efficiency_notes"),
)


def build_evaluations(verification: dict) -> list[dict]:
    """Map a verification_result dict onto submit_evaluation kwargs.

    Pure function — no SDK, no I/O — so the mapping is testable on its own.
    Fields the verifier did not produce are SKIPPED rather than defaulted:
    inventing a score for a dimension that was never assessed would put
    fabricated data on a correctness dashboard.
    """
    if not isinstance(verification, dict) or not verification:
        return []

    notes = verification.get("reasoning")
    notes = notes if isinstance(notes, dict) else {}
    evaluations: list[dict] = []

    for label, key, notes_key in _CATEGORICAL:
        value = verification.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        evaluations.append(
            {
                "label": label,
                "metric_type": "categorical",
                "value": value.strip(),
                "reasoning": str(notes.get(notes_key, "") or "")[:1000],
            }
        )

    valid = verification.get("evidence_citations_valid")
    invalid = verification.get("evidence_citations_invalid")
    if isinstance(valid, int) and isinstance(invalid, int) and not isinstance(valid, bool):
        total = valid + invalid
        # Zero citations is the evidence_unavailable path, not a division error.
        ratio = round(valid / total, 4) if total > 0 else 0.0
        evaluations.append(
            {
                "label": "ir_evidence_citation_ratio",
                "metric_type": "score",
                "value": ratio,
                "reasoning": f"{valid} valid / {invalid} invalid citations",
            }
        )

    tier = verification.get("automated_tier")
    if isinstance(tier, str) and tier.strip():
        reliable = tier.strip().upper() != "UNRELIABLE"
        evaluations.append(
            {
                "label": "ir_investigation_reliable",
                "metric_type": "boolean",
                "value": reliable,
                "assessment": "pass" if reliable else "fail",
                "reasoning": f"automated_tier={tier.strip()}",
            }
        )

    return evaluations


def submit_verification_evaluations(callback_context) -> None:
    """ADK after_agent_callback on root_agent. Never raises.

    Skips silently when Datadog is disabled or when this trace was not sampled —
    client-side sampling drops the trace before ingestion, so an evaluation
    joined to a dropped span would have nothing to attach to.
    """
    if not is_enabled():
        return None

    try:
        from ddtrace.llmobs import LLMObs
        from ddtrace.llmobs._constants import LLMObsSamplingDecision
        from ddtrace.llmobs._utils import get_llmobs_sampling_decision

        # Reuse persistence's fence-tolerant parser: the verifier is an LLM and
        # routinely wraps its JSON in ```json fences or a sentence of prose.
        from ..sub_agents.persistence import _as_dict

        verification = _as_dict(callback_context.state.get("verification_result"))
        evaluations = build_evaluations(verification)
        if not evaluations:
            logger.info("No verification scores to submit to Datadog")
            return None

        span = LLMObs._instance._current_span() if LLMObs._instance else None
        if span is None:
            logger.info("No active LLMObs span; skipping evaluation submission")
            return None

        if get_llmobs_sampling_decision(span) == LLMObsSamplingDecision.DROPPED.value:
            logger.info("Trace not sampled; skipping evaluation submission")
            return None

        exported = LLMObs.export_span(span=span)
        for evaluation in evaluations:
            LLMObs.submit_evaluation(
                span=exported,
                ml_app=resolved_ml_app(),
                eval_scope="trace",
                **evaluation,
            )
        logger.info("Submitted %d verification evaluations to Datadog", len(evaluations))
    except Exception as exc:  # observability must never break the agent
        logger.warning("Failed to submit verification evaluations: %s", exc)
    return None

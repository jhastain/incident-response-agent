"""
Persistence Agent (code-only — no LLM).

Runs LAST, after report_agent + verification_agent. Transforms pipeline state
into schema_version 2 JSON and writes it to GCS + Firestore.

This was an LlmAgent that spent ~128s having Sonnet regenerate a JSON document
from state that is already structured — the single largest sink on the ~600s
critical path, for pure bookkeeping that does no investigation. It is now a
deterministic BaseAgent: it maps existing state into the schema in code (~1s)
and calls the same write tools. Zero LLM latency, zero accuracy impact (it
never analyzed anything — it reformatted). The user already has the report and
verification before this runs, so persistence is fully off the answer path.

Writes:
  - GCS  gs://incident-summaries/<name>.json   (write_incident_file)
  - Firestore incidents/<incident_id>             (write_incident_to_firestore)
  - Firestore verification scores on that doc     (update_verification_scores)
Any write failure is logged and swallowed — persistence must never fail the run.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time as _time
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from ..app_utils import dd_tags
from ..tools.firestore_tools import (
    record_incident_contributions,
    update_verification_scores,
    write_incident_to_firestore,
)
from ..tools.gcs_tools import write_incident_file

logger = logging.getLogger(__name__)

_SEV_MAP = {"critical": "sev1", "high": "sev2", "medium": "sev3", "low": "sev4"}


# Top-level keys that identify a verifier score block (app/sub_agents/verification.py
# "Output Format"). Two must be present: a single one can appear by coincidence in a
# tool result the verifier quotes back mid-narrative, two together do not.
_VERIFICATION_MARKERS = frozenset({
    "automated_tier", "combined_tier", "diagnosis", "solution", "efficiency",
    "evidence_citations_valid", "evidence_citations_invalid", "evidence_citations_unchecked",
})
_MIN_VERIFICATION_MARKERS = 2

# Candidate provenance, used only to break score ties. A fenced block (or the whole
# string) is a deliberate emission; the outermost {...} span is a guess that can staple
# two unrelated objects together, so it loses any tie.
_SRC_EXPLICIT = 2
_SRC_SPAN = 1


def _json_candidates(s: str) -> list[tuple[int, dict]]:
    """Every dict that can be parsed out of an LLM narrative, in emission order.

    Returns (source_rank, parsed) pairs: the whole string, then the inside of each
    fence, then the outermost {...} span. Non-dict JSON (a bare string, int or list)
    is dropped here — that is what used to reach the caller and raise
    AttributeError: 'str' object has no attribute 'get'.
    """
    out: list[tuple[int, dict]] = []

    def _try(rank: int, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(parsed, dict):
            out.append((rank, parsed))

    _try(_SRC_EXPLICIT, s)
    # split("```") alternates outside/inside; [1::2] is the inside of each fence.
    for segment in s.split("```")[1::2]:
        _try(_SRC_EXPLICIT, segment)
        # Retry without an opening language tag (```json, ```python, ...).
        head, _, rest = segment.lstrip().partition("\n")
        if rest and head.strip().isalnum():
            _try(_SRC_EXPLICIT, rest)
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        _try(_SRC_SPAN, s[start:end + 1])
    return out


def _as_dict(value) -> dict:
    """Coerce a state value (pydantic model, dict, or JSON string) to a dict.

    LLM outputs are frequently JSON wrapped in a ```json ... ``` fence or with a
    sentence of prose around it, so a bare json.loads on the whole string fails.
    Takes the first parseable dict; always returns a dict, never a str/int/list.

    For `verification_result` use `_as_verification_dict` instead — that field needs
    content-based selection, not the first thing that happens to parse.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if not isinstance(value, str):
        return {}
    candidates = _json_candidates(value.strip())
    return candidates[0][1] if candidates else {}


def _as_verification_dict(value) -> dict:
    """Pick the verifier's score block out of its reasoning narrative.

    The verifier does not emit a bare document: it narrates, echoing Python snippets
    and quoting JSON tool results in their own fences, and only then writes its score.
    The previous parser read *the first fence only*, so whenever an earlier fence held
    valid JSON — a `find_similar_incidents` result, a Cloud Run description, a monitor
    definition — it returned that instead, and the real score was persisted as null.
    (Where the earlier fence was *not* valid JSON the outermost-{...}-span fallback
    usually rescued it, which is why the failure looked intermittent.)

    So select on content, not position: parse every candidate and take the one carrying
    the most verification markers. Ties break toward the later block, because the
    narrative quotes other people's JSON before it writes its own and because a verifier
    that drafts a score then corrects it means the correction. Marker-gating — rather
    than simply taking the last fence — is what keeps a trailing prose or tool-output
    fence from displacing the score.

    Returns {} when nothing carries the markers. The caller must log that, not persist
    it silently.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if not isinstance(value, str):
        return {}

    scored = [
        (len(_VERIFICATION_MARKERS.intersection(candidate)), rank, index, candidate)
        for index, (rank, candidate) in enumerate(_json_candidates(value.strip()))
    ]
    scored = [c for c in scored if c[0] >= _MIN_VERIFICATION_MARKERS]
    if not scored:
        return {}
    return max(scored, key=lambda c: c[:3])[3]


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _content_text(content) -> str:
    """Join the text parts of a genai Content into one string.

    The alert can arrive split across several parts; concatenating keeps the
    stored input byte-faithful to what the pipeline was asked to investigate.
    """
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    texts = [t for t in (getattr(p, "text", None) for p in parts) if t]
    return "\n".join(texts).strip()


def _slug(text: str, words: int = 4) -> str:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(toks[:words]) or "incident"


def _build_incident(state: dict, now: datetime.datetime) -> tuple[str, str, dict]:
    """Build (incident_id, file_name, incident_dict) deterministically from state."""
    classif = _as_dict(state.get("classification"))
    category = (classif.get("category") or "unknown").strip() or "unknown"
    severity_word = (classif.get("severity") or "").strip().lower()
    # classifier already emits sev1-4; map only if it's a word form
    severity = severity_word if severity_word.startswith("sev") else _SEV_MAP.get(severity_word, "sev3")
    service = (classif.get("service_name") or classif.get("service") or "unknown").strip() or "unknown"
    environment = classif.get("environment")
    gcp_project = classif.get("gcp_project")

    ts = now.strftime("%Y%m%d-%H%M%S")
    incident_id = f"{ts}-{category}"
    sev_n = severity.replace("sev", "") or "3"
    # The alert text comes from the invocation, NOT from the classifier: its
    # structured-output schema (AlertClassification) has no raw_text field, so
    # classif.get("raw_text") returned None on every run from 2026-07-14 onward
    # and 319 of 480 stored incidents lost the alert they were asked about.
    raw_text = state.get("alert_raw_text") or classif.get("raw_text")
    desc = _slug(raw_text or category)
    # incident_id makes the name collision-proof. Without it the name is a
    # function of (date, sev, category, service, desc) — and with desc collapsing
    # to the category whenever raw_text was null, 196 of 319 runs (61.4%)
    # overwrote an earlier object in GCS.
    file_name = (
        f"{now.strftime('%Y-%m-%d')}_sev{sev_n}_{category}_{_slug(service,2)}"
        f"_{desc}_{incident_id}.json"
    )

    verification = _as_verification_dict(state.get("verification_result"))

    incident = {
        "schema_version": 2,
        "incident_id": incident_id,
        "timestamp": now.isoformat(),
        "source": "ir-agent",
        "alert_info": {
            "raw_text": raw_text,
            "source": classif.get("alert_source") or classif.get("source"),
            "url": None,
        },
        "category": category,
        "severity": severity,
        "service": service,
        "environment": environment,
        "gcp_project": gcp_project,
        # Structured fields are preserved verbatim from the stages that produced
        # them; the report markdown is the human-facing artifact and is stored
        # whole rather than re-parsed by an LLM.
        "synthesis": _as_text(state.get("synthesis")),
        "report": _as_text(state.get("report")),
        "service_context": _as_text(state.get("service_context")),
        "past_incidents": _as_text(state.get("past_incidents")),
        "findings": {
            k: _as_text(state.get(k))
            for k in (
                "findings_cloud_run", "findings_compute_engine", "findings_gcp_logging",
                "findings_gcp_metrics", "findings_datadog", "findings_sentry",
                "findings_deploy", "findings_teams", "findings_known_issues",
            )
            if state.get(k)
        },
        "tier2_findings": _as_text(state.get("tier2_findings")),
        # Which Tier 1 agents the tree ran. tree_router logs this at INFO, which
        # never reaches Cloud Logging (root level is WARNING), so the stored
        # incident is the only record of a narrowed run.
        "tier1_routing": {
            "plan": _as_dict(state.get("tier1_plan")) or None,
            "note": _as_text(state.get("tier1_routing_note")) or None,
        } if state.get("tier1_plan") or state.get("tier1_routing_note") else None,
        "verification": verification or None,
        "resolution": {"summary": "Under investigation", "actions_taken": [], "resolved_at": None},
        "engineer_debrief": None,
    }
    # Server-side execution time: now - run_start_epoch (stamped by the classifier
    # before_agent_callback). Network-independent measure of true agent duration.
    start = state.get("run_start_epoch")
    if start:
        try:
            incident["telemetry"] = {"duration_seconds": round(_time.time() - float(start), 1)}
        except (TypeError, ValueError):
            incident["telemetry"] = None
    return incident, file_name, incident


class PersistenceAgent(BaseAgent):
    """Deterministic, code-only persistence — builds schema-v2 JSON and writes it."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = dict(ctx.session.state)
        # The alert the run was actually given. ctx.user_content is the ADK-native
        # source and is the only one that cannot be dropped by a prompt change —
        # the previous route (the classifier echoing raw_text) silently stopped
        # working when the structured-output schema landed without that field.
        if not state.get("alert_raw_text"):
            alert_text = _content_text(getattr(ctx, "user_content", None))
            if alert_text:
                state["alert_raw_text"] = alert_text
        # datetime.now is required here (real wall-clock incident id); this runs
        # server-side in the deployed container, not in a workflow sandbox.
        now = datetime.datetime.now(datetime.UTC)

        summary_lines = []
        incident_id = None
        try:
            incident, file_name, incident_dict = _build_incident(state, now)
            incident_id = incident["incident_id"]
            # Stamped here: the id is minted here and never reaches state, so
            # the Datadog plugin cannot read it.
            dd_tags.annotate_root_span(incident_id=incident_id)
            incident_json = json.dumps(incident, default=str)

            gcs_res = write_incident_file(file_name, incident_json)
            fs_res = write_incident_to_firestore(incident_id, incident_json)
            summary_lines.append(f"GCS: {file_name}")
            summary_lines.append(f"Firestore: incidents/{incident_id}")

            raw_verification = state.get("verification_result")
            verification = _as_verification_dict(raw_verification)
            if verification:
                # combined_tier = automated_tier until human feedback exists
                automated = verification.get("automated_tier") or verification.get("combined_tier")
                if automated and "combined_tier" not in verification:
                    verification = {**verification, "combined_tier": automated}
                vr = update_verification_scores(incident_id, json.dumps(verification, default=str))
                summary_lines.append(f"Verification: {automated}")
                # update_verification_scores swallows its own failures into a JSON
                # {"error": ...} string, so a Firestore rejection here is otherwise
                # indistinguishable from a success and the score is lost silently.
                if '"error"' in (vr or ""):
                    logger.error(
                        "Verification score produced but NOT persisted for %s: %s",
                        incident_id, vr,
                    )
                    summary_lines.append(f"Verification persist FAILED: {vr}")
            elif raw_verification:
                # A score was produced in-session and we could not read it. Persisting
                # null here is the defect this story exists for: never do it quietly.
                raw_text = _as_text(raw_verification)
                logger.error(
                    "Verification present in state but no score block found "
                    "(%d chars, %d fenced blocks) — persisting null. Head: %.300r Tail: %.300r",
                    len(raw_text), len(raw_text.split("```")) // 2,
                    raw_text[:300], raw_text[-300:],
                )
                summary_lines.append(
                    f"Verification UNPARSEABLE ({len(raw_text)} chars) — persisted null"
                )
            else:
                logger.error(
                    "No verification_result in state for %s — verification_agent "
                    "produced nothing; persisting null.", incident_id,
                )
                summary_lines.append("Verification MISSING from state — persisted null")
            # Guarded separately from the writes above: the tree is a learning
            # aid, the incident is the record, and losing the record to a
            # learning-write failure would be reported as a persistence bug.
            try:
                tree_res = record_incident_contributions(incident["category"], incident_json)
                summary_lines.append(f"Decision tree: {tree_res}")
                if '"error"' in (tree_res or ""):
                    logger.error(
                        "Decision-tree contribution update failed for %s: %s",
                        incident_id, tree_res,
                    )
            except Exception as tree_exc:  # never fail the run on a learning write
                logger.error(
                    "Decision-tree contribution update raised (non-fatal): %s: %s",
                    type(tree_exc).__name__, tree_exc,
                )
                summary_lines.append(
                    f"Decision tree error (non-fatal): {type(tree_exc).__name__}: {tree_exc}"
                )

            logger.info("Persistence complete: %s | %s", incident_id, gcs_res)
        except Exception as exc:  # never fail the run on a bookkeeping write
            logger.error("Persistence failed (non-fatal): %s: %s", type(exc).__name__, exc, exc_info=exc)
            summary_lines.append(f"Persistence error (non-fatal): {type(exc).__name__}: {exc}")

        text = "Incident persisted.\n" + "\n".join(summary_lines)
        # MUST set invocation_id — ADK's session service append_event rejects
        # events without it (Vertex 400: "event.invocation_id Required field is
        # not set"). Framework-driven LlmAgents get this stamped automatically;
        # a hand-built Event from a custom BaseAgent must set it explicitly.
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={"persistence_result": text}),
        )


persistence_agent = PersistenceAgent(
    name="persistence_agent",
    description="Deterministically transforms investigation results into schema_version 2 "
                "JSON and writes to GCS and Firestore (no LLM).",
)

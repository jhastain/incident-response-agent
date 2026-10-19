"""FastAPI receiver: PagerDuty incident.triggered -> IR Agent triage -> note back.

Flow:
  1. PD posts incident.triggered to POST /pagerduty/webhook.
  2. We verify the X-PagerDuty-Signature over the RAW body, then ACK 200 immediately
     (PagerDuty requires a fast 2xx; the investigation takes ~200-600s).
  3. A background task fetches the alert details, invokes the agent, and posts the
     synthesized report back onto the SAME incident as a note, prefixed with the
     verifier's confidence tier (see _confidence_banner).

Self-exclusion: we only subscribe to incident.triggered and only ADD notes (never create
or annotate-trigger), so the agent cannot loop on its own output. As defense-in-depth we
also skip events whose agent identity is our own From email.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import BackgroundTasks, FastAPI, Request, Response

from . import agent_client
from . import pagerduty_client as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pd-ir-receiver")

app = FastAPI(title="IR Agent PagerDuty Receiver")

_HANDLED = {"incident.triggered"}

_NOTE_HEADER = "🤖 **Incident Response Agent — Triage**"

# verification_agent scores every run and the score is already in hand when this note is
# composed (agent_client.investigate() drains the whole SSE stream before returning). It
# used to be dropped on the floor here. The tier is DELIVERY METADATA — how much the agent
# trusts its own write-up — so it belongs in this wrapper, not in the report body.
#
# The verifier can never award HIGH (that requires a human), so MEDIUM is the ceiling and
# the modal outcome; it must not read as a failure. LOW/UNRELIABLE must read, at a glance
# and under time pressure, as "the agent does not trust this".
_TIER_BANNERS = {
    "MEDIUM": (
        "✅ **Agent confidence: MEDIUM** — the agent checked the citations below against "
        "the tool output it collected and they hold up. MEDIUM is the highest tier the "
        "agent can award itself; HIGH requires a human to confirm."
    ),
    "LOW": (
        "⚠️ **Agent confidence: LOW** — the agent could only partly support its own "
        "conclusion from the evidence it collected. Confirm the key findings yourself "
        "before acting on them."
    ),
    "UNRELIABLE": (
        "🛑 **Agent confidence: UNRELIABLE** — the agent could NOT verify its own "
        "conclusion against tool evidence. Treat everything below as unconfirmed; do not "
        "act on it without investigating independently."
    ),
}

_TIER_MISSING = (
    "❔ **Agent confidence: not reported** — the verification step returned no verdict for "
    "this run, so nothing below has been checked against tool evidence. No warning here "
    "is not a pass."
)


def _parse_verification(raw) -> dict:
    """Coerce the verification_agent payload to a dict. Never raises.

    The verifier is an LLM: it emits JSON, but routinely inside a ```json fence or with a
    sentence of prose around it. (app/sub_agents/persistence.py learned this the hard way —
    a bare json.loads silently dropped the score on 11 of 11 runs.) The receiver ships as a
    standalone Cloud Run image with only fastapi/httpx/google-auth, so it cannot import
    that parser; this is the same three-step fallback, kept small.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    s = raw.strip()
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        pass
    if "```" in s:
        fenced = s.split("```", 2)
        if len(fenced) >= 2:
            body = fenced[1]
            if body.lstrip().lower().startswith("json"):
                body = body.lstrip()[4:]
            try:
                parsed = json.loads(body.strip())
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(s[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _verification_tier(raw) -> str:
    """Return the normalized automated_tier, or "" when the run produced no verdict.

    ~22% of stored runs have no verification block at all (ADO-200015), so "" is a normal
    outcome, not an error. The value is sanitized because it lands in a note we publish.
    """
    tier = _parse_verification(raw).get("automated_tier")
    if not isinstance(tier, str):
        return ""
    return re.sub(r"[^A-Z_]", "", tier.strip().upper())[:24]


def _confidence_banner(raw) -> str:
    """One line stating how far the agent trusts the report it is about to hand over."""
    tier = _verification_tier(raw)
    if not tier:
        return _TIER_MISSING
    known = _TIER_BANNERS.get(tier)
    if known:
        return known
    # A tier the verifier is not supposed to emit (e.g. HIGH, which requires a human).
    # Report it verbatim rather than mapping it onto a meaning it may not have.
    return (
        f"❔ **Agent confidence: {tier}** — the verification step returned a verdict this "
        "receiver does not recognize. Treat the findings below as unverified."
    )


def _build_note(result: dict) -> str:
    """Compose the PagerDuty note: header, confidence banner, then the report."""
    if not result.get("ok"):
        return (
            "🤖 Incident Response Agent — triage unavailable.\n"
            f"Error: {result.get('error')}"
        )
    return (
        f"{_NOTE_HEADER}\n"
        f"{_confidence_banner(result.get('verification'))}\n\n"
        f"{result['report']}"
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


def _process(incident_id: str, event_data: dict) -> None:
    """Background worker: fetch alert body -> investigate -> note back. Never raises
    into the request path."""
    try:
        token = pd.rest_token()
        details = pd.fetch_alert_details(incident_id, token)
        message = pd.build_alert_message(event_data, details)
        logger.info("investigating incident %s", incident_id)

        result = agent_client.investigate(message)

        note = _build_note(result)
        posted = pd.add_note(incident_id, note, token, pd.from_email())
        logger.info(
            "incident %s: note posted=%s (agent ok=%s, tier=%s)",
            incident_id,
            posted,
            result["ok"],
            _verification_tier(result.get("verification")) or "none",
        )
    except Exception:
        logger.exception("processing failed for incident %s", incident_id)


@app.post("/pagerduty/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    raw = await request.body()

    # 1. Verify signature over the RAW body before parsing.
    if not pd.verify_signature(raw, request.headers.get("X-PagerDuty-Signature"), pd.webhook_secret()):
        logger.warning("rejected webhook: bad signature")
        return Response(status_code=403)

    # 2. Parse and filter.
    try:
        event = (await request.json()).get("event", {}) or {}
    except Exception:
        return Response(status_code=400)

    event_type = event.get("event_type")
    if event_type not in _HANDLED:
        return Response(status_code=200)  # ack + ignore

    data = event.get("data", {}) or {}
    incident_id = data.get("id")
    if not incident_id:
        return Response(status_code=200)

    # Self-exclusion: don't investigate an event our own agent produced.
    agent_ref = (event.get("agent") or {})
    if (agent_ref.get("summary") or "").lower() == pd.from_email().lower():
        logger.info("skipping self-originated event on %s", incident_id)
        return Response(status_code=200)

    # 3. ACK now, do the long work off the request path.
    background.add_task(_process, incident_id, data)
    return Response(status_code=202)

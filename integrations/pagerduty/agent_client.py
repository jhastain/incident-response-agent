"""Client for invoking the deployed Incident Response Agent (Vertex AI Agent Engine).

The agent runs as a reasoningEngines resource and is invoked via two REST calls:
  1. async_create_session -> returns a session id
  2. stream_query (SSE)   -> streams ADK Event objects; the synthesized report is the
     longest text emitted by the `report_agent` author.

Auth uses Application Default Credentials (the Cloud Run runtime service account),
scoped to cloud-platform. The runtime SA needs roles/aiplatform.user on the engine's
project so it can call reasoningEngines streamQuery.
"""

from __future__ import annotations

import json
import logging
import os

import google.auth
import httpx
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

# Engine REST surface. Required, with NO default (ADO-200022).
#
# This used to default to the dev engine (projects/000000000002/.../1000000000000000002).
# That engine is alive, which makes it worse than a dead one: a receiver deployed
# without the var would accept the PagerDuty webhook, pass the HMAC check, return
# 202, triage against a stale dev build with dev tool scope, and post the note.
# Nothing anywhere would error. deploy.ps1 always passes the var; the exposure was
# a receiver stood up by any other route (a manual `gcloud run deploy`, a future
# prod receiver). Refusing to import is the loud failure that exposure needs.
_ENGINE = os.environ.get("IR_AGENT_ENGINE_URL", "").strip()
if not _ENGINE:
    raise RuntimeError(
        "IR_AGENT_ENGINE_URL is not set. It names which deployed engine (and "
        "therefore which environment) triages every PagerDuty incident this "
        "receiver handles — a wrong value produces a confident triage note from "
        "the wrong build rather than an error, so there is no safe default. "
        "deploy.ps1 sets it from the target project's engine; set it explicitly "
        "for any other deploy route."
    )
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# streamQuery has a fixed ~600s deadline; keep the client above it.
_STREAM_TIMEOUT = float(os.environ.get("IR_AGENT_STREAM_TIMEOUT", "900"))
_SESSION_TIMEOUT = 30.0
_USER_ID = os.environ.get("IR_AGENT_USER_ID", "pagerduty-webhook@acme.example")


def _token() -> str:
    """Fetch an OAuth2 access token for the runtime service account (ADC)."""
    creds, _ = google.auth.default(scopes=[_SCOPE])
    creds.refresh(Request())
    return creds.token


def _create_session(client: httpx.Client, token: str) -> str:
    """POST :query async_create_session -> session id at output.id. Retries transient
    cold-instance failures a few times (matches the e2e harness behavior)."""
    body = {"class_method": "async_create_session", "input": {"user_id": _USER_ID}}
    last = None
    for attempt in range(5):
        try:
            r = client.post(
                f"{_ENGINE}:query",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=_SESSION_TIMEOUT,
            )
            r.raise_for_status()
            sid = r.json().get("output", {}).get("id")
            if sid:
                return sid
            last = f"no session id in response: {r.text[:200]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        logger.warning("session create attempt %d failed: %s", attempt + 1, last)
    raise RuntimeError(f"session create failed after 5 attempts: {last}")


def _extract_report(sse_text_lines) -> tuple[str, str]:
    """Parse the SSE stream; return (report_markdown, verification_json_text).

    Each non-empty `data:` line is one ADK Event. The report is the LONGEST text part
    authored by report_agent (it may stream partials); verification likewise.
    """
    report = ""
    verification = ""
    for raw in sse_text_lines:
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        author = event.get("author") or ""
        for part in (event.get("content", {}) or {}).get("parts", []) or []:
            text = part.get("text")
            if not text:
                continue
            if author == "report_agent" and len(text) > len(report):
                report = text
            elif author == "verification_agent" and len(text) > len(verification):
                verification = text
    return report, verification


def investigate(alert_text: str) -> dict:
    """Run one investigation. Returns {report, verification, ok, error}.

    Blocking + long (~200-300s, up to the 600s deadline). Call this from a background
    worker, never inside the webhook request handler.
    """
    token = _token()
    with httpx.Client() as client:
        session_id = _create_session(client, token)
        body = {
            "class_method": "stream_query",
            "input": {"user_id": _USER_ID, "session_id": session_id, "message": alert_text},
        }
        try:
            with client.stream(
                "POST",
                f"{_ENGINE}:streamQuery?alt=sse",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=_STREAM_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                report, verification = _extract_report(resp.iter_lines())
        except Exception as e:
            logger.exception("streamQuery failed")
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "report": "", "verification": ""}

    if not report:
        return {"ok": False, "error": "no report produced by agent", "report": "", "verification": verification}
    return {"ok": True, "error": None, "report": report, "verification": verification}

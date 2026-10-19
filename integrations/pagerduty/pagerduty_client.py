"""PagerDuty REST helpers: signature verification, alert-body fetch, and note-back.

Kept separate from the web layer so it can be unit-tested without a running server.
Secrets are read from env (mounted from GCP Secret Manager in Cloud Run):
  PD_WEBHOOK_SECRET   - signing secret for X-PagerDuty-Signature verification
  PD_REST_API_TOKEN   - REST API token with incident write scope (for notes)
  PD_FROM_EMAIL       - a real PagerDuty user email, required in the From header
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_PD_API = "https://api.pagerduty.com"
_ACCEPT = "application/vnd.pagerduty+json;version=2"
# PagerDuty note hard cap is 65535 single-byte chars; stay well under and leave room
# for a header/footer we add around the agent report.
_NOTE_MAX = 60000

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"https?://\S+")


def _encode_semicolons_in_urls(text: str) -> str:
    """PagerDuty's linkifier terminates a URL at a literal ';', so a GCP Logs Explorer
    link (path-parameter form '/logs/query;query=...') linkifies only up to '/query' and
    the filter is left as plain text — the reader clicks through to an empty Explorer.
    '%3B' round-trips at GCP. Only URLs are touched; a ';' in prose is left alone."""
    return _BARE_URL.sub(lambda m: m.group(0).replace(";", "%3B"), text)


def _flatten_links_for_pagerduty(md: str) -> str:
    """PagerDuty notes auto-linkify bare URLs and its regex eats the trailing ')' in
    [label](url), producing [label]([url)](url). Render as 'label: url' so PagerDuty
    linkifies the bare URL cleanly.

    A URL used more than once prints its URL only on the LAST occurrence; earlier ones
    collapse to the bare label. The Links trail is the report's final section, so the
    trail stays complete and the inline citations above it stop repeating it. GCP Logs
    Explorer URLs run 200-480 chars, so a duplicated link is most of a note's bulk."""
    counts: dict[str, int] = {}
    for m in _MD_LINK.finditer(md):
        counts[m.group(2)] = counts.get(m.group(2), 0) + 1

    seen: dict[str, int] = {}

    def render(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        seen[url] = seen.get(url, 0) + 1
        return label if seen[url] < counts[url] else f"{label}: {url}"

    return _MD_LINK.sub(render, md)


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify X-PagerDuty-Signature. Header is 'v1=<hex>[,v1=<hex>...]' (multiple during
    secret rotation). HMAC-SHA256 of the RAW body bytes with the signing secret."""
    if not signature_header or not secret:
        return False
    expected = "v1=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    candidates = [s.strip() for s in signature_header.split(",")]
    return any(hmac.compare_digest(expected, c) for c in candidates)


def fetch_alert_details(incident_id: str, token: str) -> str:
    """The V3 webhook payload does NOT include the triggering alert body — only incident
    metadata. Fetch the alerts to get the diagnostic custom_details / body the agent
    needs. Returns a flattened text blob (best-effort; empty string on failure)."""
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(
                f"{_PD_API}/incidents/{incident_id}/alerts",
                headers={"Authorization": f"Token token={token}", "Accept": _ACCEPT},
            )
            r.raise_for_status()
            alerts = r.json().get("alerts", [])
    except Exception as e:
        logger.warning("fetch_alert_details failed for %s: %s", incident_id, e)
        return ""

    parts = []
    for a in alerts[:5]:
        body = a.get("body", {}) or {}
        details = body.get("details") or body.get("cef_details", {}).get("details")
        summary = (a.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        if details:
            parts.append(details if isinstance(details, str) else str(details))
    return "\n".join(parts)[:8000]


def build_alert_message(event_data: dict, alert_details: str) -> str:
    """Flatten the incident metadata + fetched alert details into the single plain-text
    string the agent expects as its `message`."""
    d = event_data
    lines = [
        f"PagerDuty incident {d.get('id')} (#{d.get('number')}): {d.get('title')}",
        f"Service: {(d.get('service') or {}).get('summary')} "
        f"| Urgency: {d.get('urgency')} "
        f"| Priority: {(d.get('priority') or {}).get('summary')}",
        f"Status: {d.get('status')} | Created: {d.get('created_at')} | {d.get('html_url')}",
    ]
    if alert_details:
        lines.append("Alert details:")
        lines.append(alert_details)
    # Flatten to one line the way the e2e harness does (newlines -> spaces).
    return " ".join(" ".join(lines).split())


def add_note(incident_id: str, content: str, token: str, from_email: str) -> bool:
    """POST /incidents/{id}/notes. The From header MUST be a real PD user email for a
    classic API token or PD returns 400 (code 1028). Truncates to the note cap."""
    content = _encode_semicolons_in_urls(_flatten_links_for_pagerduty(content))
    if len(content) > _NOTE_MAX:
        content = content[: _NOTE_MAX - 20] + "\n…[truncated]"
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(
                f"{_PD_API}/incidents/{incident_id}/notes",
                headers={
                    "Authorization": f"Token token={token}",
                    "Accept": _ACCEPT,
                    "Content-Type": "application/json",
                    "From": from_email,
                },
                json={"note": {"content": content}},
            )
            r.raise_for_status()
            return True
    except Exception as e:
        logger.exception("add_note failed for %s: %s", incident_id, e)
        return False


# --- env accessors (raise clearly if a required secret is missing) -----------------

def webhook_secret() -> str:
    return os.environ.get("PD_WEBHOOK_SECRET", "")


def rest_token() -> str:
    t = os.environ.get("PD_REST_API_TOKEN", "")
    if not t:
        raise RuntimeError("PD_REST_API_TOKEN not set")
    return t


def from_email() -> str:
    return os.environ.get("PD_FROM_EMAIL", "ir-agent@acme.example")

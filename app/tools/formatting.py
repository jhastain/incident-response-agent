"""Utility tools for formatting and parsing alert data."""

import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# Central time observes DST: CST (UTC-6) roughly November-March, CDT (UTC-5) the
# rest of the year. This was previously a fixed timezone(timedelta(hours=-6)),
# which is wrong for about two thirds of the calendar — a live 2026-07-30 report
# rendered 20:35Z as "14:35 CST" when it is 15:35 CDT, an hour off and mislabelled.
# A responder reading that looks in the wrong window of logs. The IANA zone makes
# both the offset and the CST/CDT label follow the date automatically.
CENTRAL = ZoneInfo("America/Chicago")


def format_alert_context(alert_json: str) -> str:
    """Parse and format raw alert JSON into a structured investigation context.

    Use this to convert alert payloads from PagerDuty, Datadog, or GCP Monitoring
    into a standard format for downstream agents.

    Args:
        alert_json: Raw alert JSON string from webhook or user paste.

    Returns:
        JSON string containing extracted alert signals (project, service, metric, threshold, time).
    """
    try:
        alert = json.loads(alert_json)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON"})

    context = {
        "gcp_project": None,
        "environment": None,
        "service": None,
        "metric": None,
        "threshold": None,
        "actual_value": None,
        "alert_time_utc": None,
        "alert_time_cst": None,
    }

    # Extract common fields from various alert formats
    if "incident" in alert:  # PagerDuty format
        incident = alert["incident"]
        context["service"] = incident.get("service", {}).get("summary")
        context["alert_time_utc"] = incident.get("created_at")
        if context["alert_time_utc"]:
            context["alert_time_cst"] = utc_to_cst(context["alert_time_utc"])
    elif "alert_policy_name" in alert:  # GCP Monitoring format
        context["gcp_project"] = alert.get("resource", {}).get("labels", {}).get("project_id")
        context["metric"] = alert.get("metric", {}).get("type")
        context["alert_time_utc"] = alert.get("started_at")
        if context["alert_time_utc"]:
            context["alert_time_cst"] = utc_to_cst(context["alert_time_utc"])
    elif "monitor" in alert:  # Datadog format
        monitor = alert["monitor"]
        context["service"] = monitor.get("tags", [])
        context["metric"] = monitor.get("query")
        context["alert_time_utc"] = alert.get("timestamp")
        if context["alert_time_utc"]:
            context["alert_time_cst"] = utc_to_cst(str(context["alert_time_utc"]))

    return json.dumps(context, indent=2)


def utc_to_cst(utc_timestamp: str) -> str:
    """Convert a UTC timestamp string to Central Standard Time (CST/CDT).

    Investigation Rule 9 requires all timestamps displayed to users include CST conversion.
    This handles ISO 8601 and epoch formats.

    Args:
        utc_timestamp: UTC timestamp as ISO 8601 string (e.g., '2026-06-12T14:30:00Z') or epoch seconds.

    Returns:
        Central timestamp as a readable string, labelled with the zone that
        actually applied on that date (e.g., '2026-01-12 08:30:00 CST' or
        '2026-06-12 09:30:00 CDT').
    """
    try:
        # Try parsing as epoch first
        if utc_timestamp.isdigit():
            dt = datetime.fromtimestamp(int(utc_timestamp), tz=UTC)
        else:
            # Parse ISO 8601
            dt = datetime.fromisoformat(utc_timestamp.replace("Z", "+00:00"))

        # A naive timestamp is UTC by contract (this function's whole input
        # domain); without this it would be read as local time on whatever host
        # happens to run the agent.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        local = dt.astimezone(CENTRAL)
        # %Z resolves to CST or CDT from the date — never hardcode the label.
        return local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, AttributeError):
        return f"[invalid timestamp: {utc_timestamp}]"


def parse_alert_json(raw_text: str) -> dict:
    """Extract alert JSON from a raw alert message text.

    Use this when users paste alert messages that contain JSON embedded in text.
    Attempts to find and parse the first valid JSON object or array in the text.

    Args:
        raw_text: Raw text containing JSON (from Teams message, email, etc.).

    Returns:
        Parsed JSON as a Python dict, or {"error": "..."} if no valid JSON found.
    """
    # Try to find JSON block within the text
    json_match = re.search(r"\{.*\}|\[.*\]", raw_text, re.DOTALL)
    if not json_match:
        return {"error": "No JSON found in text"}

    try:
        return json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return {"error": "Found JSON-like text but could not parse it"}

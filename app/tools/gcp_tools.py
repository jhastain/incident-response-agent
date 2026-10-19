"""GCP investigation tools for the Incident Response Agent.

Each function uses google-cloud SDK libraries to query GCP resources.
All functions return JSON-serialized strings (ADK requirement).
"""

import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import logging_v2, monitoring_v3, run_v2
from google.cloud.monitoring_v3 import TimeInterval

_MAX_OUTPUT_CHARS = 4_000


def _expected_runtime_sa() -> str:
    """The identity this agent is SUPPOSED to run as, in the current env's project.

    Deploying without `--service-account` silently falls back to the
    Google-managed reasoning-engine service agent, which holds viewer roles only
    on the agent's own project — so every cross-project query 403s. On
    2026-07-27 that produced 32 permission-denied responses and a report that
    read "NO DATA RETRIEVED" for the alerted service.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    return f"ir-agent-sa@{project}.iam.gserviceaccount.com" if project else "ir-agent-sa"


_FALLBACK_SA_MARKER = "gcp-sa-aiplatform-re"


def _runtime_identity() -> str:
    """Best-effort read of the service account this process is running as."""
    try:
        import google.auth

        creds, _ = google.auth.default()
        return getattr(creds, "service_account_email", "") or ""
    except Exception:  # identity is diagnostic only — never break a tool call over it
        return ""


def _permission_error(tool: str, project_id: str, exc: Exception) -> str:
    """Explain a 403 in terms of the runtime identity, not just the raw API error.

    A bare "permission denied" reads like a fact about the project. It is almost always
    a fact about WHICH identity is asking — and the agent previously papered over these
    with "expected; human investigators have access" instead of surfacing the cause.
    """
    text = str(exc)
    is_denied = "403" in text or "permission" in text.lower() or "denied" in text.lower()
    payload = {"error": f"{tool} failed: {exc}"}
    if not is_denied:
        return json.dumps(payload)

    identity = _runtime_identity()
    payload["permission_denied"] = True
    payload["project_queried"] = project_id
    payload["runtime_identity"] = identity or "unknown"
    if identity and _FALLBACK_SA_MARKER in identity:
        payload["diagnosis"] = (
            f"Running as the SHARED reasoning-engine service agent ({identity}), NOT "
            f"{_expected_runtime_sa()}. The deploy is missing --service-account, so this "
            "agent has no cross-project access. This is a deployment defect, not a "
            "missing grant."
        )
    else:
        payload["diagnosis"] = (
            f"Identity {identity or 'unknown'} lacks read access on {project_id}. If this "
            f"is {_expected_runtime_sa()}, the project grant is missing."
        )
    payload["instruction"] = (
        "Report this as 'NO DATA — access denied', state the identity and project, and do "
        "NOT substitute an estimate, a figure from another project, or a remembered value. "
        "An unanswerable question is a finding; an invented number is a defect."
    )
    return json.dumps(payload)


def _logs_explorer_url(project_id: str, filter_str: str) -> str:
    """Build a clickable Cloud Logging Logs Explorer deep link for a filter+project.

    Format: console.cloud.google.com/logs/query;query=<url-encoded filter>?project=<id>
    The report should cite this as the source link for any GCP-log-based finding.
    """
    return (
        "https://console.cloud.google.com/logs/query;query="
        f"{quote(filter_str, safe='')}?project={project_id}"
    )


def _cap_output(json_str: str) -> str:
    """Truncate tool output to stay within token budget."""
    if len(json_str) <= _MAX_OUTPUT_CHARS:
        return json_str
    return json_str[:_MAX_OUTPUT_CHARS] + f"\n... [TRUNCATED — {len(json_str):,} chars, showing first {_MAX_OUTPUT_CHARS:,}]"


def query_gcp_logs(
    project_id: str, filter_str: str, order_by: str = "timestamp desc", limit: int = 20
) -> str:
    """Query Google Cloud Logging for log entries matching a filter.

    Use this to search for errors, warnings, and application logs within a GCP project.
    The filter_str uses Cloud Logging filter syntax.

    Args:
        project_id: The GCP project ID to query logs from (e.g., 'webcore-prod-project').
        filter_str: Cloud Logging filter expression (e.g., 'severity>=ERROR AND resource.type="cloud_run_revision"').
        order_by: Sort order for results. Default is 'timestamp desc' (newest first).
        limit: Maximum number of log entries to return. Default is 20.

    Returns:
        JSON string containing matching log entries with timestamps, severity, and payload.
    """
    try:
        client = logging_v2.Client(project=project_id)
        entries = list(
            client.list_entries(
                filter_=filter_str,
                order_by=order_by,
                max_results=limit,
                resource_names=[f"projects/{project_id}"],
            )
        )
        results = []
        for entry in entries:
            results.append(
                {
                    "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                    "severity": entry.severity,
                    "log_name": entry.log_name,
                    "payload": str(entry.payload),
                    "resource": (
                        {"type": entry.resource.type, "labels": dict(entry.resource.labels)}
                        if entry.resource
                        else None
                    ),
                }
            )
        # Clickable Logs Explorer link for this exact query — cite as the source in the report.
        out = {"logs_explorer_url": _logs_explorer_url(project_id, filter_str), "entries": results}
        return _cap_output(json.dumps(out, default=str))
    except GoogleAPICallError as e:
        return _permission_error("query_gcp_logs", project_id, e)
    except Exception as e:
        return json.dumps({"error": f"query_gcp_logs unexpected error: {type(e).__name__}: {e}"})


def query_time_series(
    project_id: str, metric_type: str, filter_str: str = "", interval_minutes: int = 30
) -> str:
    """Query GCP Monitoring time series data for a metric.

    Use this to analyze metric trends, spikes, and historical values.
    Returns data points over the specified time interval.

    Args:
        project_id: The GCP project ID to query metrics from (e.g., 'webcore-prod-project').
        metric_type: Full metric type path (e.g., 'pubsub.googleapis.com/subscription/num_undelivered_messages').
        filter_str: Additional filter criteria using resource.labels prefix (e.g., 'resource.labels.service_name="my-service"'). Default is empty.
        interval_minutes: Time window in minutes to query (counting back from now). Default is 30.

    Returns:
        JSON string containing time series data points with timestamps and values.
    """
    try:
        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{project_id}"

        now = datetime.utcnow()
        interval = TimeInterval(
            {
                "end_time": {"seconds": int(now.timestamp())},
                "start_time": {"seconds": int((now - timedelta(minutes=interval_minutes)).timestamp())},
            }
        )

        full_filter = f'metric.type="{metric_type}"'
        if filter_str:
            full_filter += f" AND {filter_str}"

        results = client.list_time_series(
            request={
                "name": project_name,
                "filter": full_filter,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )

        series_data = []
        for series in results:
            points = []
            for point in series.points[:20]:  # Limit to 20 data points per series
                points.append(
                    {
                        "timestamp": str(point.interval.end_time) if point.interval.end_time else None,
                        "value": (
                            point.value.int64_value
                            if point.value.HasField("int64_value")
                            else point.value.double_value
                        ),
                    }
                )
            series_data.append(
                {
                    "metric": dict(series.metric.labels),
                    "resource": dict(series.resource.labels),
                    "points": points,
                }
            )

        return _cap_output(json.dumps(series_data, default=str))
    except GoogleAPICallError as e:
        return _permission_error("query_time_series", project_id, e)
    except Exception as e:
        return json.dumps({"error": f"query_time_series unexpected error: {type(e).__name__}: {e}"})


def list_alert_policies(project_id: str) -> str:
    """List all alert policies configured in a GCP project.

    Use this to discover what alerts are configured and their conditions.

    Args:
        project_id: The GCP project ID to query (e.g., 'webcore-prod-project').

    Returns:
        JSON string containing alert policy names, display names, and conditions.
    """
    try:
        client = monitoring_v3.AlertPolicyServiceClient()
        project_name = f"projects/{project_id}"

        policies = client.list_alert_policies(request={"name": project_name})

        results = []
        for policy in list(policies)[:20]:  # Limit to first 20 policies
            results.append(
                {
                    "name": policy.name,
                    "display_name": policy.display_name,
                    "enabled": policy.enabled,
                    "conditions": [
                        {
                            "name": cond.name,
                            "display_name": cond.display_name,
                        }
                        for cond in policy.conditions
                    ],
                }
            )

        return _cap_output(json.dumps(results, default=str))
    except GoogleAPICallError as e:
        return _permission_error("list_alert_policies", project_id, e)
    except Exception as e:
        return json.dumps({"error": f"list_alert_policies unexpected error: {type(e).__name__}: {e}"})


def list_cloud_run_services(project_id: str, region: str = "us-central1") -> str:
    """List all Cloud Run services in a GCP project and region.

    Use this to discover deployed services and their status.

    Args:
        project_id: The GCP project ID to query (e.g., 'webcore-prod-project').
        region: GCP region to query. Default is 'us-central1'.

    Returns:
        JSON string containing service names, URIs, and status.
    """
    try:
        client = run_v2.ServicesClient()
        parent = f"projects/{project_id}/locations/{region}"

        services = client.list_services(request={"parent": parent})

        results = []
        for service in list(services)[:20]:  # Limit to first 20 services
            results.append(
                {
                    "name": service.name,
                    "uri": service.uri,
                    "generation": service.generation,
                    "latest_ready_revision": service.latest_ready_revision,
                    "latest_created_revision": service.latest_created_revision,
                }
            )

        return _cap_output(json.dumps(results, default=str))
    except GoogleAPICallError as e:
        return _permission_error("list_cloud_run_services", project_id, e)
    except Exception as e:
        return json.dumps({"error": f"list_cloud_run_services unexpected error: {type(e).__name__}: {e}"})


def list_cloud_run_revisions(project_id: str, region: str = "us-central1", service_name: str = "") -> str:
    """List all Cloud Run revisions for a service or all services in a region.

    Use this to check revision history, traffic splits, and deployment status.

    Args:
        project_id: The GCP project ID to query (e.g., 'webcore-prod-project').
        region: GCP region to query. Default is 'us-central1'.
        service_name: Optional service name to filter revisions. If empty, lists revisions for all services.

    Returns:
        JSON string containing revision names, creation times, and serving status.
    """
    try:
        client = run_v2.RevisionsClient()

        if service_name:
            parent = f"projects/{project_id}/locations/{region}/services/{service_name}"
        else:
            parent = f"projects/{project_id}/locations/{region}"

        revisions = client.list_revisions(request={"parent": parent})

        results = []
        for revision in list(revisions)[:10]:  # Limit to first 10 revisions
            results.append(
                {
                    "name": revision.name,
                    "service": revision.service,
                    "generation": revision.generation,
                    "create_time": str(revision.create_time) if revision.create_time else None,
                    "conditions": [
                        {
                            "type": cond.type_,
                            "state": cond.state,
                            "message": cond.message,
                        }
                        for cond in revision.conditions
                    ],
                }
            )

        return _cap_output(json.dumps(results, default=str))
    except GoogleAPICallError as e:
        return _permission_error("list_cloud_run_revisions", project_id, e)
    except Exception as e:
        return json.dumps({"error": f"list_cloud_run_revisions unexpected error: {type(e).__name__}: {e}"})


def describe_cloud_run_service(project_id: str, region: str = "us-central1", service_name: str = "") -> str:
    """Get detailed information about a specific Cloud Run service.

    Use this to check service configuration, scaling settings, health, and traffic routing.

    Args:
        project_id: The GCP project ID to query (e.g., 'webcore-prod-project').
        region: GCP region where the service is deployed. Default is 'us-central1'.
        service_name: The service name to describe (required).

    Returns:
        JSON string containing service configuration, scaling settings, and status.
    """
    if not service_name:
        return json.dumps({"error": "service_name is required"})

    client = run_v2.ServicesClient()
    name = f"projects/{project_id}/locations/{region}/services/{service_name}"

    try:
        service = client.get_service(request={"name": name})

        result = {
            "name": service.name,
            "uri": service.uri,
            "generation": service.generation,
            "latest_ready_revision": service.latest_ready_revision,
            "latest_created_revision": service.latest_created_revision,
            "traffic": [
                {
                    "type": traffic.type_,
                    "revision": traffic.revision,
                    "percent": traffic.percent,
                }
                for traffic in service.traffic
            ],
            "conditions": [
                {
                    "type": cond.type_,
                    "state": cond.state,
                    "message": cond.message,
                }
                for cond in service.conditions
            ],
        }

        return _cap_output(json.dumps(result, default=str))
    except GoogleAPICallError as e:
        return _permission_error("describe_cloud_run_service", project_id, e)
    except Exception as e:
        return json.dumps({"error": f"describe_cloud_run_service unexpected error: {type(e).__name__}: {e}"})

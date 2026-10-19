"""
Alert Classification Agent

Receives raw alert text and extracts structured classification data.
This is the FIRST agent in the pipeline. It uses NO tools - pure LLM classification.
"""

import datetime as _dt
import re
import time

from google.adk.agents import LlmAgent
from google.genai import types
from pydantic import BaseModel, Field

from ..categories import CATEGORY_DESCRIPTIONS
from ..config import create_fast_model


def _stamp_run_start(callback_context):
    """Record run start (epoch seconds) so persistence can compute server-side
    execution time. Classifier is the first agent, so this marks t0."""
    if not callback_context.state.get("run_start_epoch"):
        callback_context.state["run_start_epoch"] = time.time()
    return None


# Epoch timestamps embedded in PagerDuty / Datadog monitor URLs. These are the only
# machine-precise trigger time in many alert payloads — the prose often carries just a
# RESOLVED time, which can be days later.
#
# On 2026-07-27 the classifier returned alert_timestamp="unknown" for PagerDuty #1003
# because the trigger time appeared ONLY as `from_ts=1784665701000` inside a monitor URL.
# Every downstream query then anchored on "now", and the agent produced a STRONG-EVIDENCE
# root cause for an incident six days after the one it was asked about.
_EPOCH_PARAM_RE = re.compile(
    r"\b(?:from_ts|start_ts|event_ts|link_event_ts|to_ts|end_ts)\s*=\s*(\d{10}|\d{13})\b",
    re.IGNORECASE,
)
# Window start is what anchors a query; prefer it over the end/event marks.
_START_PARAMS = ("from_ts", "start_ts")

# An explicit trigger time stated in the alert body, for sources that carry no epoch URL
# param at all. PagerDuty always supplies one ("Created: ..."), and the Teams Platform-Alerts
# harness supplies the channel post time. Without this, A1/A2/A5-style alerts (Power
# Automate / Octopus cards) fall through to "unknown" and every query anchors on NOW.
_ISO_FIELD_RE = re.compile(
    r"\b(?:alert[_ ]?time|alert[_ ]?timestamp|triggered[_ ]?at|created[_ ]?at|created|"
    r"posted[_ ]?at|fired[_ ]?at|occurred[_ ]?at)\s*[:=]\s*"
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)


def extract_epoch_timestamp(alert_text: str) -> str:
    """Pull the alert's trigger time out of embedded epoch URL params.

    Returns an ISO 8601 UTC string, or "" if the text carries no epoch param.
    Prefers a window-start param (from_ts/start_ts); falls back to the earliest
    epoch found, since the trigger precedes the resolve.
    """
    if not alert_text:
        return ""
    starts, others = [], []
    for match in _EPOCH_PARAM_RE.finditer(alert_text):
        name = match.group(0).split("=")[0].strip().lower()
        raw = int(match.group(1))
        seconds = raw / 1000 if len(match.group(1)) == 13 else float(raw)
        (starts if name in _START_PARAMS else others).append(seconds)
    candidates = starts or others
    if not candidates:
        # No epoch URL param. Fall back to an explicitly labelled time in the body
        # ("Created: 2026-07-27T13:28:00Z" from PagerDuty, or the Teams post time the
        # harness prepends). Unlabelled dates are deliberately NOT matched — the alert
        # body is full of release ids and example links that look date-shaped.
        field = _ISO_FIELD_RE.search(alert_text)
        if not field:
            return ""
        raw_iso = field.group(1).strip().replace(" ", "T")
        if raw_iso.endswith("Z"):
            raw_iso = raw_iso[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(raw_iso)
        except ValueError:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.UTC)
        return parsed.astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    chosen = min(candidates)
    try:
        return (
            _dt.datetime.fromtimestamp(chosen, _dt.UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (OverflowError, OSError, ValueError):
        return ""


class AlertClassification(BaseModel):
    """Structured output schema for alert classification."""

    category: str = Field(
        description="One of the 12 alert categories: application-errors, business-logic, deploy-release, "
                   "scheduled-jobs, data-integrity, cloud-infrastructure, sql-database, auth-security, "
                   "performance, monitoring-meta, pubsub-messaging, content-archive, or unknown"
    )
    severity: str = Field(
        description="Alert severity level using the sev1-sev4 scale: "
                   "sev1 (system down, widespread outage), sev2 (degraded performance, partial outage), "
                   "sev3 (isolated issues, warnings, threshold breaches), sev4 (informational, minor anomalies). "
                   "Must be one of: sev1, sev2, sev3, sev4"
    )
    service_name: str = Field(
        description="The affected service or platform name. Use the canonical product/platform name, NOT "
                   "the specific instance or hostname. Examples: 'WebCore' (not 'sqlprod-c1'), 'cloud-run' "
                   "(not 'recurringhighmem-us-c1'), 'subscription-service', 'accounting-service'. "
                   "Use 'unknown' if not determinable."
    )
    gcp_project: str = Field(
        description="The GCP project ID if identifiable from the alert text. Use 'unknown' if not determinable."
    )
    environment: str = Field(
        description="The environment: prod, staging, dev, or unknown. Default to prod if not determinable."
    )
    error_message: str = Field(
        description="The primary error message from the alert, verbatim if possible."
    )
    alert_timestamp: str = Field(
        description="The alert's TRIGGER/violation time in ISO 8601 UTC (e.g. '2026-06-22T14:30:00Z'). "
                   "This anchors every downstream log/metric query — getting it wrong makes the entire "
                   "investigation target the wrong time period. "
                   "Look for, in priority order: (1) epoch millisecond/second values embedded in "
                   "PagerDuty or Datadog monitor URLs — `from_ts=`, `to_ts=`, `link_event_ts=`, "
                   "`event_ts=` — these are machine-precise, PREFER them over prose; use from_ts as the "
                   "window start (13-digit values are milliseconds, 10-digit are seconds); "
                   "(2) 'Violation started', 'fired at', 'Triggered', 'First seen', 'started_at'; "
                   "(3) explicit body timestamps. "
                   "NEVER use a RESOLVED, ACK, or closed time as the alert_timestamp — those mark when a "
                   "human touched the ticket, often days after the incident, and anchoring on them sends "
                   "the whole investigation to the wrong day. "
                   "Use 'unknown' only if the alert truly contains no trigger time in any form."
    )
    alert_source: str = Field(
        description="Where the alert came from: datadog, sentry, gcp, pagerduty, teams, or unknown"
    )
    keywords: list[str] = Field(
        description="3-5 key terms for searching related issues (e.g., endpoint names, error types, service names)"
    )


# CATEGORY_DESCRIPTIONS is a pre-formatted string from categories.py
_category_table = CATEGORY_DESCRIPTIONS


CLASSIFIER_INSTRUCTION = f"""\
You are the Alert Classifier for the Incident Response Agent.

Your job is to analyze raw alert text and extract structured classification data.
You have NO tools — this is pure text analysis.

## Categories

Classify the alert into exactly one of these categories:

{_category_table}

## Environment to GCP Project Mapping

Use this table to map environment references to GCP project IDs:

| Environment | GCP Project Pattern | Common Projects |
|-------------|-------------------|-----------------|
| **Production** | `acme-production-*`, `*-prod-*` | `acme-production-100000`, `webcore-prod-project`, `webcore-search-prod` |
| **Staging** | `acme-staging-*`, `*-stag-*` | `acme-staging-project`, `webcore-staging-project` |
| **Test** | `acme-prodtest-*`, `*-test-*` | `acme-prodtest-100001` — ALWAYS map acme-prodtest-* to "test", never "staging" |
| **Sandbox** | `acme-sandbox-*`, `*-sand-*` | `acme-sandbox-project` |
| **Ephemeral** | `acme-ephemeral-*`, `webcore-ephemeral-*` | `webcore-ephemeral-project` |
| **Dev** | `*-dev-*` | `aiops-dev-project` |

If the alert mentions a specific GCP project ID, extract it exactly as written.
If the alert mentions an environment name but not a project, use the mapping above.
Default to "unknown" if neither can be determined.

## Classification Rules

- **Category**: Match keywords from the alert to the category descriptions above. If no match, use "unknown".
  - **cloud-infrastructure vs deploy-release**: Use "cloud-infrastructure" when the root issue is a GCP platform problem (Cloud Run health checks, instance failures, resource allocation, networking, scaling). Use "deploy-release" ONLY when the alert is specifically about a deployment pipeline failure, release rollback, or CI/CD error. If Cloud Run startup probes fail AFTER a deploy, the issue is infrastructure (the deploy succeeded, the platform failed to start the container).
  - **application-errors vs business-logic**: Use "application-errors" when the alert describes failures, exceptions, HTTP errors, broken functionality, or load failures — especially after a code change, PR merge, or migration. Use "business-logic" ONLY when the alert is about a business rule producing a wrong result (e.g., wrong calculation, wrong filter, incorrect pricing) with the system otherwise functioning. If users report "failed to load" or "unable to access" after a code change, that's application-errors, not business-logic.
- **Severity**: Use the sev1-sev4 scale. Map from alert text if severity is mentioned in words:
  - "sev1" = system down, widespread impact, user-facing outage (maps from "critical", "P1", "emergency")
  - "sev2" = degraded performance, error spikes, partial outage (maps from "high", "P2", "major")
  - "sev3" = isolated issues, warnings, metrics above threshold (maps from "medium", "P3", "warning")
  - "sev4" = informational, minor anomalies (maps from "low", "P4", "info")
  Severity is driven by THREE general factors, in this order. Decide these first, then
  pick the level — do not reason from the alert's own wording or from environment alone.
  1. **User impact** — how many users cannot complete work right now? None / some / most.
  2. **Blast radius** — what FRACTION of the affected fleet, pipeline or dataset is degraded?
     Count it if the alert states one (e.g. "N of M healthy", "1 of 12 steps", "2% of requests").
  3. **Data loss or corruption** — is data lost, wrong, or unrecoverable? This raises severity
     one level regardless of the other two, because it does not self-heal.

  Apply them like this:
  - A MINORITY of a fleet degraded while the majority is healthy is an ISOLATED issue → sev3,
    even when a pipeline, deploy or job is blocked by it, and even in production. "One stuck
    instance among healthy peers" is the textbook sev3 shape.
  - A MAJORITY of the fleet degraded, or a whole pipeline blocked with no retry path, or a
    user-facing error rate above a few percent → sev2.
  - EVERYTHING in the fleet down, or all users of a product area blocked → sev1.
  - No user impact, no blast radius, nothing lost — a threshold nudge, a warning, a
    single-run anomaly that already recovered → sev4.

  Severity modifiers (apply AFTER the three factors above, never instead of them):
  - Non-production environments (test, staging, sandbox, ephemeral, dev) are typically sev3
    or sev4, because no customer is affected. They are never sev1 unless the alert explicitly
    states a critical/emergency severity. Use sev2 in non-production ONLY when the blast
    radius is genuinely broad: core functionality broken for the MAJORITY of users or clients
    of that function, the MAJORITY of a fleet degraded, or a whole pipeline blocked with no
    retry path. A blocked deploy, stalled pipeline step, or failed job does NOT by itself lift
    a non-prod alert to sev2 — ask the blast-radius question first. One stuck instance among
    healthy peers, or one blocked step in a pipeline that can be retried, is an isolated issue
    (sev3) no matter which environment it is in.
  - A single service or single endpoint failing in production is sev2, not sev1 — sev1 needs
    widespread or multi-service impact.
  - Contention, blocking, or queue depth WITH an error spike but no full outage is sev2, not sev1.
  - Never raise severity because the alert text sounds urgent, names a production system, or
    was escalated by a human. Raise it only when user impact, blast radius, or data loss says so.
  Default to "sev2" if uncertain.
- **Service Name**: Use the canonical service or platform name, not the specific instance or hostname.
  Common mappings:
  - Cloud Run services (any `*-us-c1`, `*-us-w1` suffix) → "cloud-run"
  - SQL Server instances (`sqlprod*`, `sqlprod-c*`) → "WebCore"
  - WebCore, acme.example, webcore-frontend → "WebCore"
  - SubscriptionService → "subscription-service"
  - AccountingService → "accounting-service"
  - FileService, FileService → "file-service"
  Use "unknown" if not determinable.
- **GCP Project**: Extract exact project ID if mentioned. Use environment mapping if only environment name is given. Default to "unknown".
- **Environment**: Look for environment indicators (prod/production, test, staging, sandbox, ephemeral, dev). Default to "prod" if not stated.
- **Error Message**: Extract the primary error message verbatim. If multiple errors, choose the most specific one.
- **Alert Timestamp**: Extract the alert's trigger/violation time in ISO 8601 UTC. Search for "Violation started",
  "fired at", "First seen", "started_at", "Triggered", body timestamps, or ACK/RESOLVED times. Normalize to UTC
  (e.g. "June 22, 2026 at 2:30PM UTC" → "2026-06-22T14:30:00Z"). This is critical: downstream agents anchor their
  log/metric query windows on this value, so a transient alert that already self-resolved can still be investigated.
  Use "unknown" only if the alert text contains no time reference at all.
- **Alert Source**: Identify from URL patterns, headers, or explicit mentions:
  - Datadog: datadoghq.com URLs, "Datadog monitor"
  - Sentry: sentry.io URLs, "Sentry issue"
  - GCP: console.cloud.google.com URLs, "Cloud Monitoring", "GCP alert"
  - PagerDuty: pagerduty.com URLs, "PagerDuty incident"
  - Teams: teams.microsoft.com URLs, "Teams thread"
  Default to "unknown" if source is unclear.
- **Keywords**: Generate 3-5 search keywords for finding related issues. Include: service name, endpoint name (if applicable), error type, metric name, affected resource.

## Examples

**Example 1: PubSub Backlog**
```
Alert: GCP Monitoring - acme-production-100000
Subscription: projects/acme-production-100000/subscriptions/my-subscription
Metric: num_undelivered_messages
Threshold: 50,000
Actual: 259,301
```

Classification:
- category: "pubsub-messaging"
- severity: "sev2"
- service_name: "my-subscription"
- gcp_project: "acme-production-100000"
- environment: "prod"
- error_message: "Subscription backlog at 259,301 undelivered messages (threshold: 50,000)"
- alert_timestamp: "unknown"
- alert_source: "gcp"
- keywords: ["pubsub", "my-subscription", "backlog", "undelivered-messages", "acme-production-100000"]

**Example 2: Datadog Error Rate**
```
[Datadog Alert] service:acme.example env:prod
Error rate above 5% (current: 8.2%)
https://us5.datadoghq.com/monitors/12345
```

Classification:
- category: "application-errors"
- severity: "sev2"
- service_name: "WebCore"
- gcp_project: "acme-production-100000" (inferred from env:prod)
- environment: "prod"
- error_message: "Error rate at 8.2% (threshold: 5%)"
- alert_timestamp: "unknown"
- alert_source: "datadog"
- keywords: ["error-rate", "acme.example", "5xx-errors", "prod", "datadog"]

**Example 3: Sentry Issue Spike**
```
Sentry: New issue in webcore-frontend (Production)
TypeError: Cannot read property 'id' of undefined
First seen: 2026-06-12 09:30:00 UTC
Events: 523 in last hour
```

Classification:
- category: "application-errors"
- severity: "sev2"
- service_name: "WebCore"
- gcp_project: "acme-production-100000" (inferred from Production environment)
- environment: "prod"
- error_message: "TypeError: Cannot read property 'id' of undefined"
- alert_timestamp: "2026-06-12T09:30:00Z"
- alert_source: "sentry"
- keywords: ["TypeError", "webcore-frontend", "undefined-property", "sentry", "production"]

**Example 4: SQL CPU Spike**
```
SQL Server CPU > 90% on sqlprod-c1
Duration: 15 minutes
Current: 95%
Environment: Production
```

Classification:
- category: "sql-database"
- severity: "sev2"
- service_name: "WebCore"
- gcp_project: "acme-production-100000" (inferred from Production)
- environment: "prod"
- error_message: "SQL CPU at 95% for 15 minutes (threshold: 90%)"
- alert_timestamp: "unknown"
- alert_source: "datadog"
- keywords: ["sql-cpu", "sqlprod-c1", "performance", "production", "database"]

**Example 5: Content Archive pipeline no-run**
```
[Infrastructure] - ContentArchive-Configure pipeline did not run
Datadog monitor 20000002: default_zero(query2).current("1d") < 1
@ci.pipeline.name:contentarchive-configure — 0 events in last 24h
Environment: dev
```

Classification:
- category: "content-archive"
- severity: "sev3"
- service_name: "unknown" (pipeline no-run has no service; do NOT infer an unrelated service)
- gcp_project: "unknown"
- environment: "dev"
- error_message: "ContentArchive-Configure had 0 runs in 24h (monitor 20000002)"
- alert_timestamp: "unknown"
- alert_source: "datadog"
- keywords: ["contentarchive-configure", "content-archive", "pipeline", "no-run", "16605223"]

**Example 6: Total outage (sev1 — the whole fleet, all users)**
```
[Datadog Alert] service:acme.example env:prod
All 24 of 24 web instances failing health checks — 100% of requests returning 503
Customer-facing login and dashboard unavailable
Duration: 12 minutes and ongoing
```

Classification:
- category: "application-errors"
- severity: "sev1"
- service_name: "WebCore"
- gcp_project: "acme-production-100000"
- environment: "prod"
- error_message: "100% of requests returning 503; 24/24 web instances failing health checks"
- alert_timestamp: "unknown"
- alert_source: "datadog"
- keywords: ["outage", "503", "health-check", "acme.example", "prod"]

Why sev1 and not sev2: the blast radius is the ENTIRE fleet (24 of 24), not a subset, and
all users are blocked. Contrast Example 7, where a minority of the same kind of fleet is down.

**Example 7: One instance of many (sev3 — minority blast radius)**
```
[GCP Monitoring] acme-staging-project
Scheduled job fleet: 1 of 20 job workers stuck, 19 running normally
Stuck worker has not drained its queue in 40 minutes; retry is available
Environment: staging
```

Classification:
- category: "scheduled-jobs"
- severity: "sev3"
- service_name: "unknown"
- gcp_project: "acme-staging-project"
- environment: "staging"
- error_message: "1 of 20 job workers stuck; queue not drained in 40 minutes"
- alert_timestamp: "unknown"
- alert_source: "gcp"
- keywords: ["scheduled-jobs", "worker-stuck", "queue-drain", "staging", "acme-staging-project"]

Why sev3 and not sev2: 19 of 20 workers are healthy, so this is an ISOLATED issue — a
minority of the fleet. The work being blocked does not raise it; a retry path exists and no
users are affected. A blocked job, deploy, or pipeline step is NOT automatically sev2 —
score the fraction that is degraded, not the fact that something is blocked.

**Example 8: Informational anomaly (sev4 — no impact, no blast radius)**
```
[Datadog Alert] service:file-service env:prod
p99 latency 210ms, briefly above the 200ms notification threshold for 3 minutes, now recovered
Error rate unchanged at 0.01%. No user reports.
```

Classification:
- category: "performance"
- severity: "sev4"
- service_name: "file-service"
- gcp_project: "acme-production-100000"
- environment: "prod"
- error_message: "p99 latency 210ms briefly above 200ms threshold for 3 minutes (recovered)"
- alert_timestamp: "unknown"
- alert_source: "datadog"
- keywords: ["p99-latency", "file-service", "threshold-breach", "recovered", "prod"]

Why sev4 and not sev3: nothing is degraded now, no user could tell, and the breach was a
marginal, self-recovered threshold nudge. Production alone does not make an alert sev3 —
sev4 is the correct level for an informational anomaly, including in production.

## Output Format

Return ONLY the structured AlertClassification object. All fields are required.
Use "unknown" for any field that cannot be determined from the alert text.
Do NOT make assumptions beyond what the alert text explicitly or implicitly states.
"""


def _backfill_alert_timestamp(callback_context):
    """Force alert_timestamp to the epoch param embedded in the raw alert text.

    The epoch param is GROUND TRUTH and OVERRIDES the model — it is not merely a fallback
    for "unknown". Measured 2026-07-28: on two alerts the extractor returned 2026-07-21 /
    2026-07-23 while the model confidently emitted 2025-08-19 / 2025-08-20 — a year off,
    lifted from the stale example window baked into Datadog's "associated traces" help
    link (start=1724077659428 -> 2024-08-19), which appears in the alert body BEFORE the
    real link_event_ts. Every downstream query then anchored on the wrong year.

    A confidently WRONG date is worse than a missing one: "unknown" trips the
    missing-window guard, while a wrong date sails through and the report reads as fully
    self-consistent about the wrong day.
    """
    state = callback_context.state
    classification = state.get("classification")
    if not isinstance(classification, dict):
        return None
    current = str(classification.get("alert_timestamp", "")).strip()

    # The state keys below are only ever populated by callers that pre-seed them.
    # Under the normal runner path NONE of them exist, so this loop resolved to ""
    # on every run, extract_epoch_timestamp("") returned "", and the override that
    # this whole callback exists to perform silently never fired. Observed live
    # 2026-07-31: the alert carried from_ts=1785443700000 (2026-07-30T20:35:00Z),
    # the extractor reads it correctly in isolation, and the classifier still
    # emitted 2026-05-30T17:35:00Z — two months off — because the callback was
    # handed an empty string. ctx.user_content is the alert actually received.
    alert_text = ""
    for key in ("alert_raw_text", "alert_text", "alert", "user_input", "input"):
        value = state.get(key)
        if isinstance(value, str) and value:
            alert_text = value
            break
    if not alert_text:
        content = getattr(callback_context, "user_content", None)
        parts = getattr(content, "parts", None) or []
        alert_text = "\n".join(
            t for t in (getattr(p, "text", None) for p in parts) if t
        ).strip()
    recovered = extract_epoch_timestamp(alert_text)
    if recovered:
        if current and current.lower() != "unknown" and current != recovered:
            classification["alert_timestamp_model_value"] = current
            classification["alert_timestamp_source"] = (
                f"epoch-param-override (model said {current}; embedded epoch param says "
                f"{recovered} — the epoch param wins)"
            )
        else:
            classification["alert_timestamp_source"] = "epoch-param-backfill"
        classification["alert_timestamp"] = recovered
        state["classification"] = classification
    elif current and current.lower() != "unknown":
        # Nothing to check the model against. Keep its value but mark it unverified so a
        # window built on it is not mistaken downstream for a ground-truth anchor.
        classification["alert_timestamp_source"] = "model-asserted (no epoch param to verify)"
        state["classification"] = classification
    else:
        # No epoch param, and the model found no trigger time either. Measured live
        # 2026-08-07 on 5 real Platform-Alerts channel posts: 4 of 5 landed here, and the
        # classifier was RIGHT to — the only timestamps those alerts carry are RESOLVED
        # and "NOTE ADDED by <human>" times, which this module deliberately refuses to
        # anchor on (see the field description above; anchoring on a resolve time is
        # ADO-200003, "runs anchored on the wrong day").
        #
        # Leaving it at "unknown" was still wrong, because the Tier-1 prompts tell the
        # model to "fall back to the last 2 hours" and the model HAS NO CLOCK. On run 1
        # of that batch it invented a window: fetch_gcp_logs queried
        # 2024-12-20T22:00:00Z — the wrong YEAR, ~20 months in the past — and returned
        # 0 entries, which reads exactly like "nothing wrong here".
        #
        # run_start_epoch is stamped by this same agent at t0, so it is a real clock and
        # a genuine upper bound on the incident: the alert necessarily fired before the
        # run that is investigating it. For a channel-posted alert it is usually within
        # minutes. It is explicitly labelled APPROXIMATE so nothing downstream mistakes
        # it for ground truth, and so the verifier can still discount temporal claims.
        started = state.get("run_start_epoch")
        if isinstance(started, (int, float)) and started > 0:
            approx = (
                _dt.datetime.fromtimestamp(started, _dt.UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            classification["alert_timestamp"] = approx
            classification["alert_timestamp_source"] = (
                f"APPROXIMATE — run start ({approx}). The alert carried no trigger time "
                "and no epoch param; any RESOLVED/ACK/NOTE time present was deliberately "
                "NOT used. Treat every temporal claim as unverified: this bounds the "
                "incident from above, it does not date it."
            )
        else:
            classification["alert_timestamp_source"] = "MISSING — queries will anchor on NOW"
        state["classification"] = classification
    return None


# Classification is a deterministic EXTRACTION task against a fixed output schema —
# there is no value in sampling diversity, and measured live there is real cost.
#
# Measured 2026-08-11 over 124 stored incidents in Firestore (`aiops-test-project`,
# collection `incidents`), grouped by SHA-1 of the whitespace-normalised
# `alert_info.raw_text`: 22 groups had the SAME alert text run 2+ times (95 docs).
# On byte-identical input the classifier disagreed with itself at
#   severity 7.4% (7/95),  service 5.3% (5/95),  category 4.2% (4/95).
# The only sev1 in the entire corpus is one half of an unstable sev1/sev2 pair.
#
# Cause: no `generate_content_config` was passed to ANY agent in this codebase, so
# every classification ran at the provider default temperature (1.0 for Anthropic).
#
# Verified against the installed google-adk 2.5.0 / google-genai 2.13.0:
#   - `LlmAgent.generate_content_config` is `Optional[types.GenerateContentConfig]`,
#     and `flows.llm_flows.basic._build_basic_request` deep-copies it onto the
#     LlmRequest, so it reaches the model layer.
#   - `models.lite_llm._get_completion_inputs` builds `generation_params` from an
#     explicit allow-list that contains "temperature" — so it is forwarded to
#     LiteLLM, and thus to Bifrost. (That same allow-list is why `thinking_config`
#     could NOT be forwarded — see verification.py.)
#
# Scope: ONLY the classifier is pinned. The investigation agents plausibly benefit
# from some exploration in tool-call sequencing and the report agent from prose
# variety; pinning those is a system-wide behaviour change that deserves its own
# work item with its own before/after measurement. See ADO-200012.
#
# NOT a determinism guarantee: Anthropic does not promise bit-identical output at
# t=0, and it is unverified whether Bifrost forwards `temperature` unmodified to
# Vertex/Anthropic. Expect the flip rate to fall substantially, not to exactly zero.
# The post-deploy k>=5 re-measure (docs/severity-stability-measurement.md) is the
# real acceptance test — a flip at t=0 means the parameter is not reaching the model.
alert_classifier = LlmAgent(
    model=create_fast_model(),
    name="alert_classifier",
    description="Classifies incoming alerts into categories and extracts structured metadata.",
    instruction=CLASSIFIER_INSTRUCTION,
    output_key="classification",
    output_schema=AlertClassification,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    before_agent_callback=_stamp_run_start,
    after_agent_callback=_backfill_alert_timestamp,
)

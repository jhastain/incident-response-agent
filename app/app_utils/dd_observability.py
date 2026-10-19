"""Datadog Agent Observability — the AIOps agent observability pattern.

THIS FILE IS THE PATTERN. To instrument another agent: copy this file, change
ML_APP and DEFAULT_SERVICE, and call setup_datadog_observability() at startup
before any other telemetry provider is installed. Nothing else here is
incident-response specific.

Why in-code enable rather than `ddtrace-run`:
  Vertex AI Agent Engine owns the process entrypoint, so there is no command to
  wrap. In-code enable also behaves identically under Cloud Run and pytest,
  which `ddtrace-run` does not (it needs env exported before process start).
  platform-ai uses the ddtrace-run path and its env config drifted between the
  Dockerfile and the deploy YAML as a direct result.

Why ml_app is always passed explicitly:
  ml_app is the primary grouping key, one of the few tags available on ml_obs.*
  metrics, and it PROPAGATES from upstream callers. Bifrost sets
  ml_app=bifrost-gateway; inheriting that would file every IR span under the
  gateway.

Why this never raises:
  This agent is what gets called when production is broken. An observability
  library must not be able to take it down. Every failure here logs and returns.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# The internal- prefix is load-bearing: the "AI Agents Overview"
# rollup (www-030-xxx) selects members with ml_app:internal-*.
ML_APP = "ir-agent"
# A different tag; deliberately unprefixed.
DEFAULT_SERVICE = "ir-agent"
DEFAULT_SITE = "us5.datadoghq.com"
# Trace 100% by default. Trace sampling is NOT the cost lever — the paid work is
# the Datadog-hosted judges, which have their own per-evaluator sampling set in the
# Datadog UI. The two knobs multiply, so sampling here would silently divide the
# judged rate again, and would also discard the agent's own verification scores,
# which cost nothing extra to emit.
DEFAULT_SAMPLE_RATE = 1.0

_enabled = False


def is_enabled() -> bool:
    """Whether Datadog Agent Observability was successfully enabled this process."""
    return _enabled


def resolved_ml_app() -> str:
    """The ml_app these spans are actually tagged with.

    DD_LLMOBS_ML_APP is set per-deploy (test ships ir-agent), so the
    ML_APP constant is only the fallback. Anything that has to MATCH the spans
    — notably submit_evaluation, whose ml_app must equal the span's or the
    evaluation is dropped as orphaned — must resolve through here rather than
    reading ML_APP directly.
    """
    return os.environ.get("DD_LLMOBS_ML_APP") or ML_APP


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _sample_rate() -> float:
    raw = os.environ.get("DD_LLMOBS_SAMPLE_RATE")
    if not raw:
        return DEFAULT_SAMPLE_RATE
    try:
        rate = float(raw)
    except ValueError:
        logger.warning(
            "DD_LLMOBS_SAMPLE_RATE=%r is not a number; using %s", raw, DEFAULT_SAMPLE_RATE
        )
        return DEFAULT_SAMPLE_RATE
    if not 0.0 <= rate <= 1.0:
        logger.warning(
            "DD_LLMOBS_SAMPLE_RATE=%s is outside 0.0-1.0; using %s", rate, DEFAULT_SAMPLE_RATE
        )
        return DEFAULT_SAMPLE_RATE
    return rate


def _enable_llmobs(**kwargs) -> None:
    """Thin seam over LLMObs.enable so tests can assert config without a real SDK."""
    from ddtrace.llmobs import LLMObs

    LLMObs.enable(**kwargs)


def setup_datadog_observability() -> bool:
    """Enable Datadog Agent Observability. No-op unless DD_LLMOBS_ENABLED is true.

    Must be called before any other tracer provider is installed — the Agent
    Engine OTel provider fixes its resource at creation time.

    Returns True if enabled. Never raises.
    """
    global _enabled

    if not _truthy(os.environ.get("DD_LLMOBS_ENABLED")):
        logger.info("Datadog Agent Observability disabled (DD_LLMOBS_ENABLED not set)")
        return False

    api_key = os.environ.get("DD_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "DD_LLMOBS_ENABLED is set but DD_API_KEY is empty — "
            "Datadog Agent Observability NOT enabled"
        )
        return False

    # LLMObs.enable() has no `version` parameter (verified: ddtrace 4.12.0
    # _llmobs.py:855-867). Version reaches spans via DD_VERSION, which ddtrace
    # reads into config.version and stamps on every span (_llmobs.py:2122).
    os.environ.setdefault("DD_VERSION", os.environ.get("COMMIT_SHA") or "dev")

    try:
        _enable_llmobs(
            ml_app=resolved_ml_app(),
            api_key=api_key,
            site=os.environ.get("DD_SITE") or DEFAULT_SITE,
            # No Datadog Agent sidecar exists on Agent Engine, so data must go
            # straight to the intake.
            agentless_enabled=True,
            # No "dev" fallback: the env tag is what the per-environment Datadog
            # dashboards and monitors filter on (infrastructure/<env>/
            # global/datadog/*, whose queries are scoped env:<env>). Defaulting to
            # "dev" would file a test or prod deploy's spans under env:dev — polluting
            # dev's dashboards while leaving that environment's own silent, with
            # nothing in the logs to indicate it. Unset is reported as "unset" so the
            # gap is visible rather than silently mis-attributed.
            env=os.environ.get("DD_ENV") or "unset",
            service=os.environ.get("DD_SERVICE") or DEFAULT_SERVICE,
            sample_rate=_sample_rate(),
        )
    except Exception as exc:  # observability must never break the agent
        logger.warning("Datadog Agent Observability failed to enable: %s", exc)
        _enabled = False
        return False

    _enabled = True
    if not os.environ.get("DD_ENV"):
        logger.warning(
            "DD_ENV is not set — spans will be tagged env=unset and will not appear on "
            "any per-environment Datadog dashboard. Pass DD_ENV=<env> at deploy time."
        )
    logger.info(
        "Datadog Agent Observability enabled (ml_app=%s env=%s sample_rate=%s)",
        resolved_ml_app(),
        os.environ.get("DD_ENV") or "unset",
        _sample_rate(),
    )
    return True

"""Business dimensions on IR agent spans. Never raises."""

from __future__ import annotations

import contextvars
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"

_TAG_SAFE = re.compile(r"[^a-z0-9_\-:./]+")

# Tagged even when empty: a span missing the tag drops out of a group-by
# entirely, reading as no data rather than as the unknown bucket.
_ALWAYS_TAGGED = ("category", "severity", "alert_source", "outcome")


def sanitize(value: Any) -> str:
    if value is None:
        return UNKNOWN
    text = str(value).strip().lower()
    if not text:
        return UNKNOWN
    return _TAG_SAFE.sub("_", text)[:200]


def annotate_span(span, **fields: Any) -> None:
    """Tag `span` with the `iragent.` prefix. Never raises."""
    if span is None:
        return
    try:
        for key, value in fields.items():
            if value is None or value == "":
                if key not in _ALWAYS_TAGGED:
                    continue
                value = UNKNOWN
            span.set_tag(f"iragent.{key}", sanitize(value))
    except Exception as exc:
        logger.debug("Could not annotate span: %s", exc)


# Set when no tracer integration opens a run span and the Datadog plugin has to
# open its own -- that span is not activated, so current_root_span() misses it.
_fallback_root: contextvars.ContextVar = contextvars.ContextVar(
    "ir_fallback_root", default=None
)


def set_fallback_root(span) -> None:
    """Register the run root to tag when ddtrace provides none. Never raises."""
    try:
        _fallback_root.set(span)
    except Exception as exc:
        logger.debug("Could not register the fallback root span: %s", exc)


def root_span():
    """The run's root span: ddtrace's if there is one, else ours. Never raises."""
    try:
        from ddtrace.trace import tracer

        return tracer.current_root_span() or _fallback_root.get()
    except Exception as exc:
        logger.warning("Could not resolve the root span: %s", exc, exc_info=True)
        return None


def annotate_root_span(**fields: Any) -> None:
    """Tag the run's root span. Never raises."""
    annotate_span(root_span(), **fields)

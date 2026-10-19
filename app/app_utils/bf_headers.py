"""Per-request Bifrost attribution headers on the LiteLlm call.

Bifrost stores an `x-bf-lh-<key>` request header on the log record under
`metadata.<key>`, which is how bifrost_usage matches records to a run.
"""

from __future__ import annotations

import contextvars
import re

from google.adk.models.lite_llm import LiteLLMClient

# Header values must be free of CR/LF, which could forge a second header.
_HEADER_SAFE = re.compile(r"[^A-Za-z0-9_\-.:/]+")

# NOT extra_headers on the model: LiteLlm folds that into _additional_args once
# at __init__, and Agent Engine shares one agent object across concurrent
# sessions, so two investigations would overwrite each other.
_dimensions: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "bf_dimensions", default={}
)


def set_dimensions(dimensions: dict[str, str | int | None]) -> None:
    """Record this run's attribution dimensions."""
    clean: dict[str, str] = {}
    for key, value in dimensions.items():
        if value is None:
            continue
        text = _HEADER_SAFE.sub("_", str(value).strip())[:200]
        if text:
            clean[key] = text
    _dimensions.set(clean)


def headers() -> dict[str, str]:
    """This run's dimensions as `x-bf-lh-*` headers."""
    return {f"x-bf-lh-{key}": value for key, value in _dimensions.get().items()}


class AttributingLiteLLMClient(LiteLLMClient):
    """LiteLLMClient that merges this run's attribution headers in."""

    async def acompletion(self, model, messages, tools, **kwargs):
        extra = kwargs.get("extra_headers") or {}
        kwargs["extra_headers"] = {**extra, **headers()}
        return await super().acompletion(model, messages, tools, **kwargs)

    def completion(self, model, messages, tools, stream=False, **kwargs):
        extra = kwargs.get("extra_headers") or {}
        kwargs["extra_headers"] = {**extra, **headers()}
        return super().completion(model, messages, tools, stream=stream, **kwargs)

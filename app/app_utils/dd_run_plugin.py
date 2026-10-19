"""Per-sub-agent and per-run cost, tokens and business dimensions.

Naming rule: an `_est` suffix means litellm list price, no suffix means
Bifrost billed. Billed metrics are absent rather than estimated when Bifrost
cannot answer; `iragent.cost_source` says which a run produced.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from . import bf_headers, bifrost_usage, dd_observability, dd_tags

logger = logging.getLogger(__name__)

# AgentTool.run_async builds a child Runner without an invocation_id, so every
# Tier 2 agent mints a fresh one. This holds the run's real id: it is what the
# gateway records are stamped with, and what identifies the true end of the run.
_run_invocation: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ir_run_invocation", default=""
)


def _service() -> str:
    """The service name the sub-agent spans and the dashboards already use."""
    return os.environ.get("DD_SERVICE") or dd_observability.DEFAULT_SERVICE


def _new_usage() -> dict:
    return {"prompt": 0, "completion": 0, "total": 0, "model": ""}


def _cost_usd(usage: dict) -> float | None:
    """Estimated cost in USD, or None. Never raises."""
    if not usage["total"] or not usage["model"]:
        return None
    try:
        import litellm

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=usage["model"],
            prompt_tokens=usage["prompt"],
            completion_tokens=usage["completion"],
        )
        return prompt_cost + completion_cost
    except Exception as exc:
        logger.debug("Could not price usage: %s", exc)
        return None


def _write_usage(span, usage: dict) -> None:
    """Stamp the estimated tokens and cost onto `span`. Never raises."""
    if span is None:
        return
    try:
        if usage["prompt"]:
            span.set_metric("iragent.input_tokens_est", float(usage["prompt"]))
        if usage["completion"]:
            span.set_metric("iragent.output_tokens_est", float(usage["completion"]))
        if usage["total"]:
            span.set_metric("iragent.tokens_est", float(usage["total"]))
        cost = _cost_usd(usage)
        # An unpriced model gets no metric, not zero -- zero is a number a
        # dashboard sums into a wrong total.
        if cost is not None:
            span.set_metric("iragent.cost_usd_est", cost)
    except Exception as exc:
        logger.debug("Could not write usage metrics: %s", exc)


def _copy_incident_id(span) -> None:
    """Copy iragent.incident_id from the root onto `span`. Never raises."""
    try:
        root = dd_tags.root_span()
        incident_id = root.get_tag("iragent.incident_id") if root else None
        if incident_id:
            span.set_tag("iragent.incident_id", incident_id)
    except Exception as exc:
        logger.debug("Could not copy incident_id: %s", exc)


def _write_billed(span, usage) -> None:
    """Stamp billed cost, tokens and record count onto `span`. Never raises."""
    if span is None:
        return
    try:
        if usage is None:
            dd_tags.annotate_span(span, cost_source="estimate_only")
            return
        span.set_metric("iragent.cost_usd", usage.cost_usd)
        span.set_metric("iragent.tokens", float(usage.tokens))
        span.set_metric("iragent.billed_records", float(usage.records))
        dd_tags.annotate_span(span, cost_source="bifrost")
    except Exception as exc:
        logger.debug("Could not write billed usage: %s", exc)


async def _write_run_cost(root, spans: dict, invocation_id: str,
                          started_at: float) -> None:
    """Write billed usage onto the root and each sub-agent span. Never raises.

    Runs in a thread: for_run() settle-polls on Bifrost's write lag, which
    would otherwise stall the other sessions Agent Engine serves concurrently.

    `spans` are already finished. Span.finish does not freeze the tag/metric
    dicts and the trace serializes only once every span is done, so writing
    here still lands.
    """
    billed = None
    try:
        billed = await asyncio.to_thread(
            bifrost_usage.from_env,
            invocation_id=invocation_id,
            start_time=started_at,
        )
    except Exception as exc:
        logger.debug("Could not read billed usage from Bifrost: %s", exc)

    billed = billed or {}
    _write_billed(root, billed.get(bifrost_usage.RUN_TOTAL))
    for (_, agent_name), span in spans.items():
        _write_billed(span, billed.get(agent_name))


class DatadogRunPlugin(BasePlugin):
    """Emits per-sub-agent spans and stamps run dimensions. Never raises."""

    def __init__(self, name: str = "datadog_run") -> None:
        super().__init__(name=name)
        # Keyed by (invocation_id, agent_name): tier1_parallel runs children
        # concurrently and the engine serves sessions concurrently, so a
        # single slot would merge two agents' counts.
        self._spans: dict[tuple[str, str], Any] = {}
        self._usage: dict[tuple[str, str], dict] = {}
        self._run_usage: dict[str, dict] = {}
        self._run_started: dict[str, float] = {}
        self._tool_calls: dict[tuple[str, str], int] = {}
        self._run_tool_calls: dict[str, int] = {}
        # Finished spans, held so the billed figures can be written onto them
        # once the run-wide Bifrost lookup returns.
        self._finished: dict[tuple[str, str], Any] = {}
        # Run roots this plugin opened, keyed by the run's invocation id. Only
        # populated when no tracer integration provided one.
        self._run_roots: dict[str, Any] = {}

    @staticmethod
    def _key(callback_context) -> tuple[str, str]:
        return (callback_context.invocation_id, callback_context.agent_name)

    def _close_orphans(self, invocation_id: str) -> None:
        """Finish sub-agent spans whose after_agent_callback never ran.

        A Tier 1 agent gated off by tier1_parallel returns Content from its
        before_agent_callback, which sets end_invocation and makes ADK return
        before the after callbacks (google/adk/agents/base_agent.py:300-313).
        With partial flush off, one unfinished span holds back the ENTIRE trace,
        so the run root and every sibling never ship.
        """
        run_id = _run_invocation.get() or invocation_id
        for key in [k for k in self._spans if k[0] in (run_id, invocation_id)]:
            span = self._spans.pop(key)
            self._usage.pop(key, None)
            self._tool_calls.pop(key, None)
            span.set_tag("iragent.skipped", "true")
            _copy_incident_id(span)
            span.finish()
            self._finished[(run_id, key[1])] = span
            logger.warning(
                "Sub-agent %s never reached after_agent_callback; closing its "
                "span so the trace can ship", key[1]
            )

    def _pop_finished(self, invocation_id: str) -> dict:
        """This run's finished spans, removed from the shared dict."""
        mine = {k: v for k, v in self._finished.items() if k[0] == invocation_id}
        for k in mine:
            del self._finished[k]
        return mine

    @staticmethod
    def _is_run_root(agent, callback_context) -> bool:
        """Whether this is the real end of the run.

        Both halves are needed. parent_agent is None for Tier 2 agents too --
        AgentTool-wrapped, never in a sub_agents list -- and the invocation id
        matches for every sequential child, since they share the run's.
        """
        return (
            agent.parent_agent is None
            and callback_context.invocation_id == _run_invocation.get()
        )

    async def before_agent_callback(self, *, agent, callback_context) -> None:
        try:
            from ddtrace.trace import tracer

            key = self._key(callback_context)
            self._usage[key] = _new_usage()
            self._tool_calls[key] = 0
            self._run_usage.setdefault(callback_context.invocation_id, _new_usage())
            self._run_started.setdefault(callback_context.invocation_id, time.time())

            # First agent of the run claims the id; Tier 2 agents run under a
            # child Runner with a fresh one and must not overwrite it.
            run_invocation_id = _run_invocation.get() or callback_context.invocation_id
            claimed_here = not _run_invocation.get()
            if claimed_here:
                _run_invocation.set(run_invocation_id)

            dd_tags.annotate_root_span(invocation_id=run_invocation_id)

            # Set on every agent boundary: a ContextVar set in one ADK task does
            # not reach a sibling, and tier1_parallel runs its children
            # concurrently. invocation_id is the RUN's, so Tier 2 spend lands on
            # this run rather than being dropped as unmatched.
            bf_headers.set_dimensions({
                "invocation_id": run_invocation_id,
                "agent": bifrost_usage.AGENT_NAME,
                "sub_agent": callback_context.agent_name,
            })

            root = tracer.current_root_span()
            if root is None and claimed_here:
                # No tracer integration opened a run span (the google-adk patch
                # is off), so the run needs one of its own or every sub-agent
                # span is parentless and the run rollup has nowhere to land.
                root = tracer.start_span(
                    "ir.run",
                    service=_service(),
                    resource=agent.name,
                    activate=False,
                )
                self._run_roots[run_invocation_id] = root
                # persistence.py tags the root through dd_tags and has no handle
                # on this plugin, so the span has to be reachable from there.
                dd_tags.set_fallback_root(root)
                dd_tags.annotate_root_span(invocation_id=run_invocation_id)
            root = root or self._run_roots.get(run_invocation_id)
            if root is None:
                return None
            # activate=False: activating would interleave concurrent Tier 1
            # children on one thread-local span stack.
            span = tracer.start_span(
                "ir.sub_agent",
                child_of=root,
                service=root.service,
                resource=agent.name,
                activate=False,
            )
            span.set_tag("iragent.sub_agent", agent.name)
            span.set_tag("iragent.invocation_id", run_invocation_id)
            self._spans[key] = span
        except Exception as exc:
            logger.warning("Could not open the sub-agent span: %s", exc, exc_info=True)
        return None

    async def after_model_callback(self, *, callback_context, llm_response) -> None:
        """Accumulate one response into the sub-agent's and the run's totals."""
        try:
            metadata = getattr(llm_response, "usage_metadata", None)
            if metadata is None:
                return None
            prompt = int(getattr(metadata, "prompt_token_count", 0) or 0)
            completion = int(getattr(metadata, "candidates_token_count", 0) or 0)
            total = int(getattr(metadata, "total_token_count", 0) or 0) or (
                prompt + completion
            )
            model = getattr(llm_response, "model_version", None)

            key = self._key(callback_context)
            for usage in (
                self._usage.setdefault(key, _new_usage()),
                self._run_usage.setdefault(
                    callback_context.invocation_id, _new_usage()
                ),
            ):
                usage["prompt"] += prompt
                usage["completion"] += completion
                usage["total"] += total
                if model:
                    usage["model"] = model
        except Exception as exc:
            logger.debug("Could not record usage: %s", exc)
        return None

    async def before_tool_callback(self, *, tool, tool_args, tool_context) -> None:
        """Count one tool call against the sub-agent that made it."""
        try:
            key = self._key(tool_context)
            self._tool_calls[key] = self._tool_calls.get(key, 0) + 1
            invocation_id = tool_context.invocation_id
            self._run_tool_calls[invocation_id] = (
                self._run_tool_calls.get(invocation_id, 0) + 1
            )
        except Exception as exc:
            logger.debug("Could not count the tool call: %s", exc)
        return None

    async def after_agent_callback(self, *, agent, callback_context) -> None:
        try:
            key = self._key(callback_context)
            span = self._spans.pop(key, None)
            usage = self._usage.pop(key, None) or _new_usage()
            tool_calls = self._tool_calls.pop(key, 0)
            if span is not None:
                _write_usage(span, usage)
                span.set_metric("iragent.tool_calls", float(tool_calls))
                # Span tags do not inherit parent to child, so copy the run
                # identity across. persistence.py mints incident_id near the end
                # of the run, so spans that close before it carry only
                # invocation_id and join to the root through that.
                _copy_incident_id(span)
                span.finish()
                # Keyed by the RUN's id, not this agent's: Tier 2 agents run
                # under a fresh one, and other sessions are in this dict too.
                self._finished[(
                    _run_invocation.get() or callback_context.invocation_id,
                    callback_context.agent_name,
                )] = span

            # Re-stamped every boundary so a run that dies mid-Tier-1 is still
            # tagged. Tags overwrite, so repeating is free.
            self._stamp_dimensions(callback_context)

            # The run ends when the agent that claimed the invocation id
            # finishes. parent_agent is None for Tier 2 agents too -- they are
            # wrapped in AgentTool and never appear in a sub_agents list -- so
            # that predicate would fire once per Tier 2 agent.
            if self._is_run_root(agent, callback_context):
                run_usage = self._run_usage.pop(
                    callback_context.invocation_id, None
                ) or _new_usage()
                started_at = self._run_started.pop(
                    callback_context.invocation_id, None
                ) or time.time()
                self._close_orphans(callback_context.invocation_id)
                root = dd_tags.root_span()
                _write_usage(root, run_usage)
                if root is not None:
                    root.set_metric("iragent.tool_calls", float(
                        self._run_tool_calls.pop(callback_context.invocation_id, 0)
                    ))
                dd_tags.annotate_root_span(outcome="completed")
                finished = self._pop_finished(callback_context.invocation_id)
                await _write_run_cost(
                    root, finished, callback_context.invocation_id, started_at
                )
                self._finish_own_root(callback_context.invocation_id)
                # The one line that proves from logs alone that this run shipped
                # spans; absence of Datadog data is otherwise undiagnosable.
                logger.warning(
                    "IR run telemetry: %d sub-agent spans closed, own_root=%s",
                    len(finished), root is not None,
                )
        except Exception as exc:
            logger.warning("Could not close the sub-agent span: %s", exc, exc_info=True)
        return None

    async def on_agent_error_callback(self, *, agent, callback_context, error) -> None:
        try:
            key = self._key(callback_context)
            span = self._spans.pop(key, None)
            self._usage.pop(key, None)
            self._tool_calls.pop(key, None)
            # Root only: a Tier 1 child failing does not end the run, and
            # dropping the start time would narrow the /api/logs window to
            # nothing for the sub-agents still running.
            if self._is_run_root(agent, callback_context):
                self._run_started.pop(callback_context.invocation_id, None)
                self._run_usage.pop(callback_context.invocation_id, None)
                self._run_tool_calls.pop(callback_context.invocation_id, None)
                self._close_orphans(callback_context.invocation_id)
                self._pop_finished(callback_context.invocation_id)
            if span is not None:
                span.set_exc_info(type(error), error, error.__traceback__)
                _copy_incident_id(span)
                span.finish()
            dd_tags.annotate_root_span(outcome="failed")
            # After the tag, so a root this plugin opened carries the outcome.
            if self._is_run_root(agent, callback_context):
                self._finish_own_root(callback_context.invocation_id)
        except Exception as exc:
            logger.warning("Could not record the sub-agent error: %s", exc, exc_info=True)
        return None

    def _finish_own_root(self, invocation_id: str) -> None:
        """Close the run root this plugin opened, if it opened one."""
        root = self._run_roots.pop(invocation_id, None)
        if root is not None:
            dd_tags.set_fallback_root(None)
            root.finish()

    @staticmethod
    def _stamp_dimensions(callback_context) -> None:
        """Tag the root span from state. incident_id comes from persistence.py,
        which mints it -- it never reaches state."""
        from ..sub_agents.persistence import _as_dict

        classification = _as_dict(callback_context.state.get("classification"))
        dd_tags.annotate_root_span(
            category=classification.get("category"),
            severity=classification.get("severity"),
            alert_source=classification.get("alert_source"),
            service_name=classification.get("service_name"),
            environment=classification.get("environment"),
        )

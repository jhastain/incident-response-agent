"""This run's billed cost and tokens, read back from the Bifrost log store.

API shape: https://docs.getbifrost.ai/api-reference/logging/get-logs
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Both filters are required: one virtual key bills every run, and Agent Engine
# serves sessions concurrently, so a time window alone merges two live runs.
_META_INVOCATION = "invocation_id"
_META_AGENT = "agent"
_META_SUB_AGENT = "sub_agent"

AGENT_NAME = "incident-response-agent"

# Key under which the whole-run total is returned alongside the per-sub-agent
# entries. Empty string cannot collide with an agent name.
RUN_TOTAL = ""

_PAGE = 500
_MAX_PAGES = 10
_HTTP_TIMEOUT = 15

# Must exceed Bifrost's write lag (~6s measured). Poll faster and two reads can
# both land before the final write, which looks settled but is not.
_SETTLE_POLL_SECONDS = 8.0

DEFAULT_RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class Usage:
    cost_usd: float
    tokens: int
    records: int


def _iso(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def _get_page(base_url: str, logs_key: str, params: dict) -> dict | None:
    url = base_url.rstrip("/") + "/api/logs?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {logs_key}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _sum_matching(logs: list, invocation_id: str, agent: str, into: dict) -> None:
    """Accumulate this run's records into `into`, keyed by sub_agent.

    Only `status == "success"` counts. A record still in `processing` carries
    cost 0 but would increment the match count, which makes the settle check
    call the run finished early.
    """
    for record in logs:
        metadata = record.get("metadata") or {}
        if metadata.get(_META_INVOCATION) != invocation_id:
            continue
        if metadata.get(_META_AGENT) != agent:
            continue
        if record.get("status") != "success":
            continue
        cost = float(record.get("cost") or 0)
        tokens = int((record.get("token_usage") or {}).get("total_tokens") or 0)
        for key in (metadata.get(_META_SUB_AGENT) or RUN_TOTAL, RUN_TOTAL):
            bucket = into.setdefault(key, [0.0, 0, 0])
            bucket[0] += cost
            bucket[1] += tokens
            bucket[2] += 1
            if key == RUN_TOTAL:
                break


def _query_once(base_url, logs_key, invocation_id, agent, start_time, end_time):
    """One pass over the window. Returns (usage or None, records seen).

    The count is every record in the window, ours or not -- it is what
    separates an empty gateway from a metadata filter that matched nothing.
    """
    params = {
        "limit": _PAGE,
        "start_time": _iso(start_time),
        "end_time": _iso(end_time if end_time is not None else time.time()),
    }
    totals: dict[str, list] = {}
    seen = 0
    for page in range(_MAX_PAGES):
        params["offset"] = page * _PAGE
        payload = _get_page(base_url, logs_key, params)
        if not payload:
            break
        logs = payload.get("logs") or []
        if not logs:
            break
        seen += len(logs)
        _sum_matching(logs, invocation_id, agent, totals)
        total = ((payload.get("pagination") or {}).get("total_count")) or 0
        if params["offset"] + len(logs) >= total:
            break
    if not totals:
        return None, seen
    return {
        key: Usage(cost_usd=cost, tokens=tokens, records=records)
        for key, (cost, tokens, records) in totals.items()
    }, seen


def for_run(
    *,
    base_url: str,
    logs_key: str,
    invocation_id: str,
    agent: str = AGENT_NAME,
    start_time: float,
    end_time: float | None = None,
    retry_seconds: float = DEFAULT_RETRY_SECONDS,
) -> dict[str, Usage] | None:
    """This run's billed usage per sub-agent, or None. BLOCKING -- use a thread.

    The whole-run total is under RUN_TOTAL; every other key is a sub-agent name.
    ONE query for the run, grouped locally -- /api/logs has no metadata filter,
    so a per-sub-agent query is not possible.

    Polls until the record count stops growing: Bifrost writes each record
    seconds after its own response, so returning on first sight undercounts.
    At the deadline a partial count is returned.
    """
    # Each miss is logged distinctly: absent billed cost is otherwise
    # indistinguishable from a bad key, a bad URL, or headers that never
    # arrived, and all four read as cost_source=estimate_only on the span.
    if not logs_key:
        logger.warning(
            "Bifrost billed cost unavailable: BIFROST_LOGS_API_KEY is unset. "
            "This endpoint takes the management key, not BIFROST_API_TOKEN.")
        return None
    if not base_url:
        logger.warning(
            "Bifrost billed cost unavailable: no gateway URL. Set "
            "BIFROST_BASE_URL, or BIFROST_LLM_URL ending in /v1.")
        return None
    if not (invocation_id and agent):
        logger.warning(
            "Bifrost billed cost unavailable: invocation_id=%r agent=%r -- "
            "both are required to match this run's records.",
            invocation_id, agent)
        return None

    deadline = time.monotonic() + max(0.0, retry_seconds)
    settled = None
    seen = 0
    answered = False
    while True:
        try:
            usage, seen = _query_once(base_url, logs_key, invocation_id, agent,
                                      start_time, end_time)
            answered = True
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Bifrost /api/logs returned HTTP %s (%s) for %s -- billed cost "
                "will be absent.", exc.code, exc.reason, base_url)
            usage, answered = None, False
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError, OSError) as exc:
            logger.warning(
                "Bifrost /api/logs lookup failed against %s: %s", base_url, exc)
            usage, answered = None, False

        if usage is not None and settled is not None:
            if usage[RUN_TOTAL].records == settled[RUN_TOTAL].records:
                total = usage[RUN_TOTAL]
                logger.warning(
                    "Bifrost billed this run: %.5f USD, %d tokens, %d records "
                    "across %d sub-agents.",
                    total.cost_usd, total.tokens, total.records, len(usage) - 1)
                return usage
        if usage is not None:
            settled = usage

        if time.monotonic() >= deadline:
            if settled is None and answered:
                # The distinction that matters: the gateway answered, but not
                # one record carried our invocation_id -- the x-bf-lh-* headers
                # did not arrive. Reads identically to an unreachable gateway.
                logger.warning(
                    "Bifrost billed cost unavailable: %d log records in the "
                    "window, NONE matching invocation_id=%s agent=%s. Either "
                    "no request was billed yet, or the x-bf-lh-* attribution "
                    "headers never reached the gateway.",
                    seen, invocation_id, agent)
            elif settled is not None:
                total = settled[RUN_TOTAL]
                # Only a budget that allowed a second poll can be cut short.
                unsettled = (
                    " -- count UNSETTLED at the deadline, so it may undercount"
                    if retry_seconds > _SETTLE_POLL_SECONDS else "")
                logger.warning(
                    "Bifrost billed this run: %.5f USD, %d tokens, %d records "
                    "across %d sub-agents%s.",
                    total.cost_usd, total.tokens, total.records,
                    len(settled) - 1, unsettled)
            return settled
        time.sleep(_SETTLE_POLL_SECONDS)


def base_url_from_env() -> str:
    """The gateway root. /api/logs sits beside /v1, not under it."""
    explicit = os.environ.get("BIFROST_BASE_URL", "").strip()
    if explicit:
        return explicit
    llm_url = os.environ.get("BIFROST_LLM_URL", "").strip().rstrip("/")
    if llm_url.endswith("/v1"):
        return llm_url[: -len("/v1")]
    return llm_url


def logs_key_from_env() -> str:
    """The /api/logs credential.

    NOT BIFROST_API_TOKEN -- that is the virtual key; this endpoint takes a
    management-scoped one. Expected unset until ADO-200026 lands a read-scoped
    key, in which case for_run() returns None.
    """
    return os.environ.get("BIFROST_LOGS_API_KEY", "").strip()


def from_env(**kwargs) -> dict[str, Usage] | None:
    """for_run() with gateway and key from the environment. BLOCKING."""
    return for_run(
        base_url=base_url_from_env(),
        logs_key=logs_key_from_env(),
        **kwargs,
    )

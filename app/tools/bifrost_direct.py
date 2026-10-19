"""Deterministic Bifrost executeToolCode client (no LLM, no ADK McpToolset).

Tier B fetch layer: given a Starlark/Python code string like
    result = datadog_token.search_datadog_logs(query="...", limit=10)
run it against Bifrost's executeToolCode meta-tool over raw StreamableHTTP MCP,
and return the text result. No listToolFiles/getToolDocs discovery dance — the
caller (planner) already knows the downstream call. Multiple codes run
concurrently via asyncio.to_thread (Bifrost handles concurrent sessions;
verified 9/9).

Each call opens its own short-lived MCP session (initialize -> tools/call),
which is what makes it safe to run many concurrently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from ..config import get_bifrost_settings

logger = logging.getLogger(__name__)

# --- Sentry boolean-query guard -------------------------------------------------
#
# Sentry's issue search rejects OR/AND outright (HTTP 400) instead of degrading, so a
# boolean query returns ZERO issues — indistinguishable from a healthy service.
#
# This guard originally lived in sentry_tierb.fetch_sentry, which was wrong: it only
# covered the one agent whose *name* is about Sentry. Measured live 2026-08-07, run 5
# of the Platform-Alerts batch, `deploy_correlation` emitted
#     sentry_token.search_issues(..., query="auth OR Auth0 OR login OR assertion")
# through fetch_deploy and got the 400. Four Tier-B modules call bifrost_execute_many
# (datadog, deploy_correlation, known_issues, sentry) and any of them can address any
# MCP server, so the guard belongs at the executor — the one chokepoint every query
# passes through — not per agent.
#
# Datadog is deliberately untouched: its language DOES support OR/AND, and a live
# session sent 8 boolean Datadog queries with zero rejections. Splitting those would
# break working queries. Hence matching on the sentry_token.search_issues CALL rather
# than on the presence of a `query=` kwarg.
_QUERY_ARG = re.compile(r'(query\s*=\s*")([^"]*)(")')
_SENTRY_SEARCH = re.compile(r"\bsentry_token\s*\.\s*search_issues\b")

# Bare OR/AND as words, so "ANDROID" or an id containing "OR" is left alone.
_BOOLEAN_OP = re.compile(r"\b(?:OR|AND)\b")


def split_boolean_sentry_query(query: str) -> list[str]:
    """Split one boolean Sentry query into the several queries Sentry can answer.

    Sentry search is implicit-AND on space-separated terms and offers no OR, so
    separate searches are the only way to express "match any of these".

    Filter tokens that qualify the whole group (`level:error`, `environment:prod`)
    are re-attached to every part, so `level:error (Redis OR timeout)` becomes
    `level:error Redis` and `level:error timeout` rather than losing the filter.
    """
    stripped = query.replace("(", " ").replace(")", " ")
    parts = [p.strip() for p in _BOOLEAN_OP.split(stripped) if p.strip()]
    if len(parts) < 2:
        return [query]

    tokenized = [p.split() for p in parts]

    # A shared filter is a `key:value` token sitting alongside other terms — written
    # once, meant for the whole group. A part that is ONLY a filter
    # (`level:error OR level:alert`) is a genuine alternative being searched for, not
    # a qualifier, so it must not be hoisted.
    filters: list[str] = []
    for tokens in tokenized:
        if len(tokens) == 1:
            continue
        for token in tokens:
            if ":" in token and token not in filters:
                filters.append(token)

    out: list[str] = []
    for tokens in tokenized:
        terms = [t for t in tokens if t not in filters]
        rebuilt = " ".join(filters + terms).strip()
        if rebuilt and rebuilt not in out:
            out.append(rebuilt)
    return out or [query]


def expand_boolean_sentry_queries(codes: list[str]) -> tuple[list[str], list[str]]:
    """Rewrite any boolean sentry_token.search_issues call into single-term calls.

    Returns (codes_to_run, notes). Prompt rules already tell the models not to do
    this; the rewrite exists because a prompt rule is advisory and the failure it
    prevents is silent — a rejected query yields no data and reads as "no errors".
    """
    expanded: list[str] = []
    notes: list[str] = []

    for code in codes:
        if not _SENTRY_SEARCH.search(code):
            expanded.append(code)
            continue
        match = _QUERY_ARG.search(code)
        if not match or not _BOOLEAN_OP.search(match.group(2)):
            expanded.append(code)
            continue

        original = match.group(2)
        variants = split_boolean_sentry_query(original)
        if len(variants) < 2:
            expanded.append(code)
            continue

        for variant in variants:
            expanded.append(code[: match.start(2)] + variant + code[match.end(2) :])
        notes.append(
            f"Rewrote a boolean Sentry query into {len(variants)} separate queries, because "
            f"Sentry rejects OR/AND with HTTP 400 and returns nothing. "
            f'Original: "{original}" -> {variants}'
        )

    return expanded, notes

# Async HTTP (httpx) — NOT sync urllib in a thread. The fetch runs inside the ADK
# streaming event loop alongside 8 other concurrent agents; blocking urllib (even
# via to_thread) contended the runtime threadpool and wedged the whole run.
# httpx.AsyncClient yields to the loop properly so many fetches interleave.
_TIMEOUT = httpx.Timeout(60.0, connect=30.0)


def _parse(raw: str, resp_headers) -> tuple[str | None, dict]:
    text = raw
    for line in raw.splitlines():
        if line.startswith("data:"):
            text = line[5:].strip()
            break
    sid = resp_headers.get("mcp-session-id")
    try:
        return sid, json.loads(text)
    except json.JSONDecodeError:
        return sid, {"_raw": text}


async def _abifrost_execute(client: httpx.AsyncClient, url: str, token: str, code: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        r0 = await client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "tierb-fetch", "version": "1"}},
        })
        sid, _ = _parse(r0.text, r0.headers)
        h = dict(headers)
        if sid:
            h["mcp-session-id"] = sid
        r1 = await client.post(url, headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "executeToolCode", "arguments": {"code": code}},
        })
        _, res = _parse(r1.text, r1.headers)
        content = res.get("result", {}).get("content", [])
        if content:
            return "\n".join(item.get("text", "") for item in content)[:6000]
        if "error" in res:
            return f"ERROR: {json.dumps(res['error'])[:500]}"
        return json.dumps(res)[:2000]
    except Exception as exc:
        logger.error("bifrost_execute failed: %s: %s", type(exc).__name__, exc)
        return f"ERROR: {type(exc).__name__}: {exc}"


async def bifrost_execute_many(codes: list[str]) -> list[str]:
    """Run many executeToolCode `code` strings CONCURRENTLY (async). Order preserved.

    Order is preserved but the LENGTH may grow: a boolean Sentry query is rewritten
    into several single-term queries here (see expand_boolean_sentry_queries), because
    Sentry rejects OR/AND with a 400 and returns nothing. Callers that zip results back
    against their original input list must use `bifrost_execute_many_annotated`, which
    also returns the codes actually run.
    """
    results, _, _ = await bifrost_execute_many_annotated(codes)
    return results


async def bifrost_execute_many_annotated(
    codes: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Run codes concurrently. Returns (results, codes_run, notes).

    `codes_run` is what was actually sent, which may be longer than `codes` after a
    boolean Sentry rewrite — zip against it, never against the original list. `notes`
    describes any rewrite so a caller can surface it; a substitution the reader cannot
    see is how a silent failure gets replaced by a silent success.
    """
    if not codes:
        return [], [], []
    settings = get_bifrost_settings()
    if not settings.api_token:
        return ["ERROR: no Bifrost token"] * len(codes), list(codes), []

    to_run, notes = expand_boolean_sentry_queries([str(c) for c in codes])
    for note in notes:
        logger.warning("bifrost_execute_many: %s", note)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            *(_abifrost_execute(client, settings.mcp_url, settings.api_token, c) for c in to_run)
        )
    return list(results), to_run, notes

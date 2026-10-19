"""Session inspection tools for verification cross-referencing.

Provides access to raw tool call results from the current session to prevent
the verification agent from hallucinating evidence by comparing actual outputs
against claimed facts in the investigation report.
"""

import json
import os
import re

import google.auth
import google.auth.transport.requests
import requests
from google.adk.tools import ToolContext
from google.api_core.exceptions import GoogleAPICallError

# Evidence budget. The old cap was 25 *list items* — and because calls and responses
# were emitted as separate items, 25 items was only ~12 tool calls. Worse, the slice was
# chronological, so the infra_context/doc-loading preamble that every run starts with ate
# the whole budget and every diagnostic RESULT (fetch_gcp_logs, fetch_datadog,
# fetch_gcp_metrics, fetch_sentry, ...) fell off the end. Measured on the live test engine
# 2026-08-07: sessions carried 48-62 entries, 25 survived, and the verifier reported
# "Session tool output truncated" as its reason for tiering UNRELIABLE.
#
# The budget is now in characters, which is what actually bounds the prompt, and entries
# are call+result PAIRS so an argument list is never separated from the result it produced
# (check (c), "did the query constrain the window", needs both side by side).
_MAX_EVIDENCE_CHARS = 60000

# Per-result cap. Raised from 500: at 500 chars a metrics payload was cut before its
# data points, which is exactly the number the verifier is asked to confirm.
_RESULT_HEAD_CHARS = 900

# Per-argument cap. The window a query ran against lives in its ARGUMENTS, not its
# result — across the observed corpus no result carried a from_ts/to_ts field, while a
# `search_datadog_logs(from=..., to=...)` call carried its window past the old 200-char
# cut and lost it. 400 is the smallest cap under which no observed call loses its time
# parameters.
_ARG_VALUE_CHARS = 400

# --- search_session_evidence budget ---------------------------------------------------
#
# The retrieval above pre-pays a flat 60K to show a FIXED slice of everything. Measured on
# the 5-run batch of 2026-08-07 the raw tool output was 148K-259K chars, so that slice was
# 23-40% of the evidence, and three different truncation schemes (per-result budget
# division, fragment skimming, number-window snippets) each raised the rendered payload to
# ~58K and still surfaced the cited value in only 2 of 5 runs. At 4:1 no truncation scheme
# can win; the budget is the wrong knob.
#
# Searching inverts the economics: the verifier names the value it wants to confirm and
# pays a few hundred characters for the window around it, so cost scales with the number
# of citations actually checked rather than with the size of the session.
_SEARCH_WINDOW_CHARS = 300      # context kept on EACH side of a hit
_SEARCH_MAX_MATCHES = 20        # windows returned; match_count still reports the true total
_SEARCH_MAX_CHARS = 12000       # hard ceiling on the rendered response
_SEARCH_MAX_PER_RESULT = 3      # so one 250K blob cannot consume every match slot
_SEARCH_MIN_QUERY_CHARS = 2

# Results the verifier's OWN evidence retrieval produced. Searching these would let a value
# be "confirmed" by an earlier echo of the retrieval rather than by the diagnostic tool that
# supposedly produced it — circular, and precisely the fabrication this gate exists to stop.
# Run 4 of the 2026-08-07 batch really does contain the cited `200` inside a recorded
# get_session_tool_outputs result, so this is a live hazard, not a hypothetical one.
_SELF_REFERENTIAL_TOOLS = frozenset({
    "get_session_tool_outputs",
    "search_session_evidence",
})

# Tools that set up context but are never cited as evidence for a root cause. They are
# allocated budget only after every diagnostic pair has been, rather than first.
_LOW_PRIORITY_TOOLS = frozenset({
    "get_service_context",
    "load_infra_context",
    "listToolFiles",
    "getToolDocs",
    "list_incident_files",
    "read_incident_file",
    "get_triage_guidance",
})


def _resolve_engine_context() -> tuple[str, str, str]:
    """Resolve (project, location, engine_id) for the RUNNING engine.

    Agent Engine does not inject REASONING_ENGINE_ID, but it DOES inject APP_URL,
    which embeds the full resource path of the engine currently executing:
        https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1/projects/
        <proj>/locations/<loc>/reasoningEngines/<id>/api
    Parsing APP_URL is the only self-referential source that cannot go stale.

    A hardcoded engine-id fallback is deliberately NOT provided: a wrong id
    silently returns "session does not exist", which the verification agent
    previously mistook for "no evidence to check" and graded around. Failing
    loudly is required for the verifier to be trustworthy.

    Also note GOOGLE_CLOUD_LOCATION is 'global' on the deployed engine, which is
    the wrong host for the sessions API — the location must come from APP_URL,
    not that env var.
    """
    app_url = os.environ.get("APP_URL", "")
    m = re.search(
        r"/projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)"
        r"/reasoningEngines/(?P<engine>[^/]+)",
        app_url,
    )
    if m:
        return m.group("project"), m.group("location"), m.group("engine")

    # Explicit override only — no stale default.
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("REASONING_ENGINE_LOCATION", "")
    engine = os.environ.get("REASONING_ENGINE_ID", "")
    if project and location and engine:
        return project, location, engine
    raise RuntimeError(
        "cannot resolve the running engine: APP_URL is unparseable and "
        "GOOGLE_CLOUD_PROJECT/REASONING_ENGINE_LOCATION/REASONING_ENGINE_ID are not all set"
    )


def get_session_tool_outputs(tool_context: ToolContext, session_id: str = "") -> str:
    """Retrieve raw tool call results from the current session's events.

    Used by the verification agent to cross-reference report claims against
    actual tool outputs, preventing hallucinated evidence from being trusted.

    Args:
        tool_context: Injected by ADK. Supplies the REAL session id, so the
            verifier never has to know or guess it.
        session_id: Ignored unless non-empty. Kept only as an explicit override
            for tests/debugging.

    Returns:
        JSON string. Each entry pairs a call with the result it produced, so the
        verifier can compare a citation's window against the query's arguments.
        `evidence_coverage` always states how many calls were shown of how many ran.

        Two distinct failure signals, which must NOT be collapsed:
        - {"evidence_unavailable": true} — nothing was retrievable. No citation can
          be checked; the verifier treats this as "cannot verify" and tiers UNRELIABLE.
        - {"evidence_partial": true} — evidence WAS read, but some lower-priority calls
          were omitted to bound the prompt. This must not produce UNRELIABLE; treating
          it as if it were the case above is what downgraded sound runs.
    """
    try:
        # Resolve the session id from the live invocation. The verifier previously
        # had NO source for this: nothing injected a {session_id} into its prompt and
        # include_contents="none" removed every other route, so the model invented the
        # literal string "current" and every lookup 404'd. The gate then tripped on
        # every single run, which looked like a working gate but meant zero citations
        # were ever actually validated.
        # Session resolution, the local-session fallback (local eval runs and tests have
        # no APP_URL and no remote session to fetch) and the Vertex sessions fetch all
        # live in _load_session_events, shared with search_session_evidence so both tools
        # read byte-identical evidence.
        events, resolved, failure = _load_session_events(tool_context, session_id)
        if failure is not None:
            return failure
        return _render(resolved, events)

    except requests.exceptions.RequestException as e:
        return _evidence_unavailable(f"get_session_tool_outputs failed: {e}")
    except GoogleAPICallError as e:
        return _evidence_unavailable(f"get_session_tool_outputs failed: {e}")
    except Exception as e:
        return _evidence_unavailable(
            f"get_session_tool_outputs unexpected error: {type(e).__name__}: {e}"
        )


def search_session_evidence(
    tool_context: ToolContext, query: str, session_id: str = ""
) -> str:
    """Search this session's raw tool outputs for a specific value.

    Use this to CONFIRM a citation before ruling it invalid. `get_session_tool_outputs`
    shows a bounded slice of every result; this searches the FULL text of all of them and
    returns only the windows around each hit, so a value buried 8,000 characters into a
    20,000-character result is still findable at a cost of a few hundred characters.

    Args:
        tool_context: Injected by ADK. Supplies the real session id.
        query: The literal value to look for — e.g. "6901", "context canceled",
            "WEBCORE-DOTNET-1A2B", "23:40:46". Matching is case-insensitive substring;
            it is NOT a regex, so punctuation is matched literally.
        session_id: Ignored unless non-empty. Explicit override for tests/debugging.

    Returns:
        JSON string with `match_count` (matches across the whole session), `matches`
        (each with `tool`, `index`, `agent` and the surrounding `window`), and
        `results_searched`.

        A zero-match response sets `found: false` and says how many results were searched.
        That is genuine evidence of ABSENCE — unlike a truncated view, the full text of
        every result was examined — and it SUPPORTS an INVALID ruling.
    """
    try:
        cleaned = (query or "").strip()
        if len(cleaned) < _SEARCH_MIN_QUERY_CHARS:
            return json.dumps({
                "error": (
                    f"query must be at least {_SEARCH_MIN_QUERY_CHARS} characters; "
                    "search for the specific cited value, e.g. '6901' or 'context canceled'"
                ),
                "search_failed": True,
            })

        events, _resolved, failure = _load_session_events(tool_context, session_id)
        if failure is not None:
            return failure

        return _render_search(cleaned, _pair_calls_with_results(events))

    except requests.exceptions.RequestException as e:
        return _evidence_unavailable(f"search_session_evidence failed: {e}")
    except GoogleAPICallError as e:
        return _evidence_unavailable(f"search_session_evidence failed: {e}")
    except Exception as e:
        return _evidence_unavailable(
            f"search_session_evidence unexpected error: {type(e).__name__}: {e}"
        )


def _render_search(query: str, pairs: list) -> str:
    """Scan every result in full and return only the windows that matched.

    Searched text is the COMPLETE result string, never the truncated `result_summary` —
    reading the cut view would reproduce exactly the blind spot this tool exists to remove.
    """
    if not pairs:
        return _evidence_unavailable("the session contains no tool calls at all")

    needle = query.lower()
    matches: list[dict] = []
    match_count = 0
    results_searched = 0
    spent = 0
    capped = False

    # Diagnostic results are scanned first so a generic value ("200", "184") cannot have
    # its window slots consumed by the setup preamble before the tool the citation
    # actually rests on is reached. Every result is still SEARCHED and still counted;
    # only the order in which windows are allocated changes.
    searchable = [
        p for p in pairs
        if p["result"] is not None and p["tool"] not in _SELF_REFERENTIAL_TOOLS
    ]
    ordered = (
        [p for p in searchable if p["tool"] not in _LOW_PRIORITY_TOOLS]
        + [p for p in searchable if p["tool"] in _LOW_PRIORITY_TOOLS]
    )

    for pair in ordered:
        result_text = pair["result"]
        results_searched += 1

        hits = _find_all(result_text.lower(), needle)
        match_count += len(hits)

        # Counting continues across EVERY result even once the window budget is spent.
        # Stopping early would understate `match_count` at exactly the moment the capped
        # message promises it is the true total.
        for offset in hits[:_SEARCH_MAX_PER_RESULT]:
            if len(matches) >= _SEARCH_MAX_MATCHES:
                capped = True
                break
            window = _window_around(result_text, offset, len(query))
            entry = {
                "index": pair["index"],
                "agent": pair["agent"],
                "tool": pair["tool"],
                "args": pair["args"],
                "offset": offset,
                "result_chars": len(result_text),
                "window": window,
            }
            cost = len(json.dumps(entry))
            if spent + cost > _SEARCH_MAX_CHARS and matches:
                capped = True
                break
            matches.append(entry)
            spent += cost

    # Allocation was priority-first; presentation is chronological, so the verifier still
    # reads a timeline.
    matches.sort(key=lambda m: m["index"])

    payload: dict = {
        "query": query,
        "found": bool(match_count),
        "match_count": match_count,
        "matches_shown": len(matches),
        "results_searched": results_searched,
        "tools_searched": sorted({p["tool"] for p in searchable}),
        "matches": matches,
    }

    if match_count:
        payload["instruction_to_verifier"] = (
            f"'{query}' WAS found in the raw output of {len(matches)} shown "
            f"result(s) ({match_count} total occurrence(s)). The full text of every result "
            "was searched, not the truncated view. This citation is SUPPORTED on check (b) — "
            "do NOT rule it invalid for absence. Still apply check (c): confirm the "
            "`args` of the matching call constrain the window the citation claims."
        )
    else:
        # The whole point of the tool. A truncated view cannot distinguish "absent" from
        # "past the cut"; a completed full-text scan can, so this is a real negative.
        payload["instruction_to_verifier"] = (
            f"'{query}' was NOT found. The COMPLETE text of all {results_searched} tool "
            "results in this session was searched — this is not a truncation blind spot, "
            "it is evidence of absence. Treat this as INVALID for check (b) unless the "
            "report cites the value in a different form (rounded, reformatted, or a "
            "different unit) — if so, search again for that form before ruling."
        )

    if capped:
        payload["search_capped"] = True
        payload["instruction_to_verifier"] += (
            f" NOTE: results were capped at {len(matches)} windows; `match_count` "
            f"({match_count}) is the true number of occurrences found."
        )

    return json.dumps(payload, indent=2)


def _find_all(haystack: str, needle: str) -> list[int]:
    """Every offset of `needle` in `haystack`, non-overlapping.

    Deliberately operates on the raw string. These results are JSON-serialized and contain
    ZERO real newlines — run 5's 19,752-char result has none — so any line-based approach
    silently does nothing while appearing to work.
    """
    offsets: list[int] = []
    start = 0
    while True:
        found = haystack.find(needle, start)
        if found == -1:
            return offsets
        offsets.append(found)
        start = found + len(needle)


def _window_around(text: str, offset: int, needle_len: int) -> str:
    """Slice the context surrounding a hit, marking any elision at the edges."""
    start = max(0, offset - _SEARCH_WINDOW_CHARS)
    end = min(len(text), offset + needle_len + _SEARCH_WINDOW_CHARS)
    window = text[start:end]
    if start > 0:
        window = "..." + window
    if end < len(text):
        window = window + "..."
    return window


def _load_session_events(tool_context, session_id: str) -> tuple[list, str, str | None]:
    """Resolve the session and return (events, session_id, None), or ([], "", error payload).

    Shared by the overview retrieval and the search so both read byte-identical evidence
    from the same source, deployed or local. The verifier has no source for the session id
    — nothing injects one into its prompt — so a guessed literal like "current" is
    discarded here in favour of the id the live invocation carries.
    """
    resolved = (session_id or "").strip()
    if not resolved or resolved.lower() in {"current", "current_session", "unknown", "none"}:
        session = getattr(tool_context, "session", None)
        resolved = getattr(session, "id", "") or ""
    if not resolved:
        return [], "", _evidence_unavailable(
            "could not resolve the current session id from the tool context"
        )

    # Off Agent Engine (local eval runs, tests) there is no APP_URL and no remote session
    # to fetch. The in-process session already holds every event, so read it directly
    # rather than reporting "no evidence" — a local run that produced 31 real tool calls
    # was being auto-tiered UNRELIABLE purely because the transport was missing, which
    # silently invalidates any accuracy measured locally. The deployed path stays
    # authoritative whenever APP_URL is present.
    if not os.environ.get("APP_URL"):
        local_events = _local_session_events(tool_context)
        if local_events:
            return local_events, resolved, None

    project, location, reasoning_engine_id = _resolve_engine_context()
    parent = f"projects/{project}/locations/{location}/reasoningEngines/{reasoning_engine_id}"
    session_name = f"{parent}/sessions/{resolved}"

    credentials, _ = google.auth.default()
    credentials.refresh(google.auth.transport.requests.Request())

    url = f"https://{location}-aiplatform.googleapis.com/v1beta1/{session_name}/events"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("sessionEvents", []), resolved, None


def _local_session_events(tool_context) -> list[dict]:
    """Normalize the in-process ADK session's events to the REST event shape.

    ADK holds events as pydantic objects with snake_case fields; the Vertex
    sessions API returns camelCase JSON. Converting here means the parser below
    stays single-sourced — the verifier sees byte-identical structure whether the
    run is local or deployed, so local scoring is faithful to production.
    """
    session = getattr(tool_context, "session", None)
    raw_events = getattr(session, "events", None) or []

    normalized: list[dict] = []
    for event in raw_events:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) or []
        out_parts: list[dict] = []
        for part in parts:
            call = getattr(part, "function_call", None)
            if call is not None:
                # ADK carries an id on the call/response pair too. Passing it through
                # lets the pairing below match on id rather than falling back to name
                # order, which matters when the same tool is called several times
                # concurrently and the responses come back interleaved.
                out_parts.append({
                    "functionCall": {
                        "id": getattr(call, "id", None),
                        "name": getattr(call, "name", "unknown"),
                        "args": dict(getattr(call, "args", None) or {}),
                    }
                })
            resp = getattr(part, "function_response", None)
            if resp is not None:
                out_parts.append({
                    "functionResponse": {
                        "id": getattr(resp, "id", None),
                        "name": getattr(resp, "name", "unknown"),
                        "response": dict(getattr(resp, "response", None) or {}),
                    }
                })
        if out_parts:
            normalized.append({
                "author": getattr(event, "author", "unknown"),
                "content": {"parts": out_parts},
            })
    return normalized


def _render(session_id: str, events: list[dict]) -> str:
    """Build the verifier payload from events in the REST shape."""
    try:
        pairs = _pair_calls_with_results(events)

        if not pairs:
            # Events exist but none of them are tool calls. There is nothing to
            # validate a citation against, so this is the same condition as no
            # evidence at all — never let it render as an empty-but-successful
            # payload, which the verifier would read as "checked, found clean".
            return _evidence_unavailable(
                "the session contains no tool calls at all"
            )

        selected, omitted = _select_within_budget(pairs)
        return json.dumps(
            _build_payload(session_id, len(events), pairs, selected, omitted), indent=2
        )

    except requests.exceptions.RequestException as e:
        return _evidence_unavailable(f"get_session_tool_outputs failed: {e}")
    except GoogleAPICallError as e:
        return _evidence_unavailable(f"get_session_tool_outputs failed: {e}")
    except Exception as e:
        return _evidence_unavailable(
            f"get_session_tool_outputs unexpected error: {type(e).__name__}: {e}"
        )


def _pair_calls_with_results(events: list) -> list:
    """Flatten session events into one entry per tool call, result attached.

    Pairing is by the function-call id the REST API echoes on the response; a name
    match is the fallback, which is also the only route for the local in-process
    path, whose normalized events carry no id. Keeping the call and its result in
    one entry is what lets the verifier run its window check — the arguments prove
    what the query asked for, the result proves what came back.
    """
    entries: list[dict] = []
    by_id: dict[str, dict] = {}
    pending_by_name: dict[str, list] = {}

    for event in events:
        content = event.get("content", {})
        parts = content.get("parts", [])
        if not parts:
            continue
        author = event.get("author", "unknown")

        for part in parts:
            if "functionCall" in part:
                func_call = part["functionCall"]
                name = func_call.get("name", "unknown")
                entry = {
                    "index": len(entries),
                    "agent": author,
                    "tool": name,
                    "args": _truncate_dict(func_call.get("args", {})),
                    "result": None,
                }
                entries.append(entry)
                call_id = func_call.get("id")
                if call_id:
                    by_id[call_id] = entry
                pending_by_name.setdefault(name, []).append(entry)

            if "functionResponse" in part:
                func_resp = part["functionResponse"]
                name = func_resp.get("name", "unknown")
                response_obj = func_resp.get("response", {})
                result_text = json.dumps(response_obj) if response_obj else ""

                target = by_id.get(func_resp.get("id"))
                if target is None:
                    queue = pending_by_name.get(name) or []
                    target = next((e for e in queue if e["result"] is None), None)
                if target is None:
                    # A result with no matching call still counts as evidence.
                    target = {
                        "index": len(entries),
                        "agent": "unknown",
                        "tool": name,
                        "args": {},
                        "result": None,
                    }
                    entries.append(target)
                target["result"] = result_text

    return entries


def _select_within_budget(pairs: list) -> tuple[list, list]:
    """Choose which pairs to render in full, spending the character budget on evidence.

    Diagnostic tools are allocated first so that a run whose setup preamble is long
    cannot starve the results the report actually cites. Ordering within each group
    stays chronological, and the selection is re-sorted before returning, so the
    verifier still reads a timeline.
    """
    diagnostic = [p for p in pairs if p["tool"] not in _LOW_PRIORITY_TOOLS]
    low = [p for p in pairs if p["tool"] in _LOW_PRIORITY_TOOLS]

    selected: list[dict] = []
    omitted: list[dict] = []
    spent = 0
    for pair in diagnostic + low:
        rendered = _render_pair(pair)
        cost = len(json.dumps(rendered))
        if spent + cost > _MAX_EVIDENCE_CHARS and selected:
            omitted.append(pair)
            continue
        selected.append(rendered)
        spent += cost

    selected.sort(key=lambda p: p["index"])
    return selected, omitted


def _render_pair(pair: dict) -> dict:
    """Render one call+result entry, preserving numbers and time windows."""
    result_text = pair["result"]
    if result_text is None:
        summary = None
        status = "no_result_recorded"
    else:
        summary = _truncate(result_text, _RESULT_HEAD_CHARS)
        status = "ok"

    return {
        "index": pair["index"],
        "agent": pair["agent"],
        "tool": pair["tool"],
        "args": pair["args"],
        "result_summary": summary,
        "result_status": status,
    }


def _build_payload(
    session_id: str, event_count: int, pairs: list, selected: list, omitted: list
) -> dict:
    """Assemble the tool response, stating evidence coverage explicitly.

    `evidence_partial` is deliberately a DIFFERENT signal from `evidence_unavailable`.
    Unavailable means nothing was retrievable and no citation can be checked — the
    anti-fabrication gate. Partial means real evidence was read and some was not shown;
    that must not produce the same verdict, or a good investigation gets graded as if
    it had invented its data.
    """
    with_results = sum(1 for p in pairs if p["result"] is not None)
    payload = {
        "session_id": session_id,
        "total_events": event_count,
        "tool_call_count": len(pairs),
        "tool_calls_shown": len(selected),
        "tool_results_available": with_results,
        "evidence_coverage": f"{len(selected)} of {len(pairs)} tool calls shown in full",
        "tool_calls": selected,
    }
    if omitted:
        payload["evidence_partial"] = True
        payload["omitted_tools"] = sorted({p["tool"] for p in omitted})
        payload["instruction_to_verifier"] = (
            f"Evidence WAS retrieved: {len(selected)} of {len(pairs)} tool calls are shown "
            f"in full, including their arguments and results. {len(omitted)} lower-priority "
            "calls were omitted to bound this prompt. This is NOT `evidence_unavailable` — "
            "do NOT set automated_tier=UNRELIABLE on account of it. Judge the citations you "
            "CAN check, mark only genuinely uncheckable ones invalid, and state your coverage "
            "(e.g. 'checked 14 of 31 tool calls') in your notes."
        )
    return payload


def _evidence_unavailable(message: str) -> str:
    """Signal that raw evidence could NOT be read.

    The flag is explicit because the verification agent previously received a
    bare {"error": ...}, kept scoring anyway, and returned 9/9 VALID citations
    without ever having seen a tool output.
    """
    return json.dumps({
        "error": message,
        "evidence_unavailable": True,
        "instruction_to_verifier": (
            "Raw tool evidence could NOT be retrieved. You cannot validate any citation. "
            "Set evidence_citations_valid=0, count every citation as unverifiable, and set "
            "automated_tier=UNRELIABLE. Do NOT infer validity from the report's wording."
        ),
    })


def _truncate(text: str, max_len: int = 500) -> str:
    """Truncate text to max_len characters with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... [truncated, {len(text)} chars total]"


def _truncate_dict(d: dict, max_keys: int = 5) -> dict:
    """Truncate dictionary to max_keys entries, summarizing values."""
    result = {}
    for i, (k, v) in enumerate(d.items()):
        if i >= max_keys:
            result["..."] = f"{len(d) - max_keys} more keys"
            break
        result[k] = _truncate(str(v), _ARG_VALUE_CHARS) if isinstance(v, str) else v
    return result

"""Evidence retrieval must work off Agent Engine, or local runs cannot be scored.

Observed live 2026-07-31 on a local `tests/eval/local_run.py` execution: 381.9s,
31 tool calls, a complete report — and the verifier returned
`automated_tier: UNRELIABLE`, `evidence_citations_valid: 0`,
`evidence_citations_invalid: 47`, with the note "APP_URL unparseable; execution
context not set".

Cause: `get_session_tool_outputs` resolved the session only through `APP_URL` and
then fetched events from the Vertex `reasoningEngines/.../sessions/{id}/events`
REST endpoint. Both exist only on a deployed Agent Engine. Under `InMemoryRunner`
there is no APP_URL and no remote session, so every local run hit
`_evidence_unavailable` and auto-tiered UNRELIABLE no matter how good the report.

That is a harness defect, not an agent defect — but it silently invalidates any
accuracy measurement taken locally, which is where this project does its
iteration. The verifier's own scoring rules make the failure look like a real
verdict rather than a missing capability.

The fix prefers the in-process session (authoritative, already in memory, no
credentials required) and falls back to the REST path when deployed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from google.adk.events import Event
from google.genai import types

from app.tools.session_tools import get_session_tool_outputs


def _tool_event(author: str, call: str, args: dict, result: dict) -> Event:
    """An event carrying one function call and its response, as ADK records it."""
    return Event(
        invocation_id="inv-local",
        author=author,
        content=types.Content(
            role="model",
            parts=[
                types.Part(function_call=types.FunctionCall(name=call, args=args)),
                types.Part(
                    function_response=types.FunctionResponse(name=call, response=result)
                ),
            ],
        ),
    )


def _ctx_with(events: list[Event]) -> SimpleNamespace:
    session = SimpleNamespace(id="local-session-1", events=events)
    return SimpleNamespace(session=session)


EVENTS = [
    _tool_event(
        "gcp_logging",
        "query_gcp_logs",
        {"project_id": "acme-production-100000", "filter_str": 'severity="ERROR"'},
        {"entries": 1247, "window": "2026-07-30T20:00:00Z/2026-07-30T21:00:00Z"},
    ),
    _tool_event(
        "datadog_observability",
        "search_datadog_logs",
        {"query": "service:WebCore status:error"},
        {"total": 29877},
    ),
]


class TestInProcessEvidence:
    def test_local_session_events_are_read_without_app_url(self, monkeypatch):
        """No APP_URL, no reasoning engine — evidence must still be retrievable."""
        monkeypatch.delenv("APP_URL", raising=False)
        monkeypatch.delenv("REASONING_ENGINE_ID", raising=False)

        out = json.loads(get_session_tool_outputs(_ctx_with(EVENTS)))

        assert not out.get("evidence_unavailable"), out
        assert out["tool_call_count"] == 2

    def test_tool_names_args_and_results_all_survive(self, monkeypatch):
        """The verifier's three checks need the call, its arguments, and its
        result. A payload missing any one of them cannot validate a citation:
        the time-window check in particular is decided by the ARGUMENTS.

        Entries are call+result PAIRS, so the arguments and the number they
        produced are on the same object and cannot be separated by a cut."""
        monkeypatch.delenv("APP_URL", raising=False)

        out = json.loads(get_session_tool_outputs(_ctx_with(EVENTS)))

        entries = {e["tool"]: e for e in out["tool_calls"]}

        assert set(entries) == {"query_gcp_logs", "search_datadog_logs"}
        # Argument survives verbatim — this is what proves the query was
        # window/severity constrained. Assert on the parsed value, not on a
        # json.dumps blob, where quotes come back escaped.
        assert entries["query_gcp_logs"]["args"]["filter_str"] == 'severity="ERROR"'
        # Result survives — this is the number a citation would quote — and it is
        # attached to the call that produced it.
        assert "29877" in entries["search_datadog_logs"]["result_summary"]

    def test_no_events_is_still_unavailable(self, monkeypatch):
        """An empty session means nothing to verify. That must keep reporting
        evidence_unavailable — the anti-fabrication gate depends on it and must
        not be weakened by this change."""
        monkeypatch.delenv("APP_URL", raising=False)

        out = json.loads(get_session_tool_outputs(_ctx_with([])))

        assert out.get("evidence_unavailable") is True

    def test_deployed_path_still_preferred_when_app_url_present(self, monkeypatch):
        """Off-Engine support must not shadow the deployed path. With APP_URL set
        the REST fetch is attempted; when it fails we surface that failure rather
        than silently substituting a partial in-memory view."""
        monkeypatch.setenv(
            "APP_URL",
            "https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1"
            "/projects/p/locations/us-central1/reasoningEngines/123/api",
        )

        def _boom(*_a, **_k):
            raise RuntimeError("REST path attempted")

        monkeypatch.setattr("app.tools.session_tools.requests.get", _boom)

        out = json.loads(get_session_tool_outputs(_ctx_with(EVENTS)))

        assert out.get("evidence_unavailable") is True
        # Either message proves the deployed REST path was taken: without
        # application-default credentials the credential lookup fails before
        # the request is made.
        assert (
            "REST path attempted" in out["error"]
            or "DefaultCredentialsError" in out["error"]
        ), out["error"]

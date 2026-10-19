"""A decision-tree write must never cost us the incident record.

`record_incident_contributions` swallows its own failures into an {"error": ...}
string, so the only way into persistence's except branch is a defect that
escapes that -- an import error, a client raising outside the transaction
helper. If the wrapper were wrong, a decision-tree bug would present as a lost
incident document and be diagnosed as a persistence bug.

Drives the real `_run_async_impl`: a test that re-implements the agent's write
ordering and asserts against its own copy would pass whatever persistence does.

Patch `app.sub_agents.persistence.record_incident_contributions`, the name the
module bound at import, not the definition in `firestore_tools` -- patching the
definition leaves the bound reference intact and the real function still runs.
"""

from __future__ import annotations

import asyncio
import json
import types as pytypes

import app.sub_agents.persistence as persistence


class _Ctx:
    """The slice of InvocationContext that PersistenceAgent actually reads."""

    def __init__(self, state: dict):
        self.session = pytypes.SimpleNamespace(state=state)
        self.invocation_id = "inv-test-287188"
        self.user_content = None


def _state() -> dict:
    return {
        "classification": json.dumps({
            "category": "sql-database",
            "severity": "sev2",
            "service_name": "WebCore",
            "environment": "prod",
        }),
        "alert_raw_text": "[Datadog] SQL blocking chain on WebCore",
        "synthesis": "Blocking chain confirmed via monitor `10000001`.",
        "report": "Mechanism: blocking chain, per monitor `10000001`.",
        "findings_datadog": "Monitor `10000001` shows a blocking chain.",
        "verification_result": json.dumps({
            "diagnosis": "STRONG",
            "automated_tier": "MEDIUM",
        }),
    }


def _drive(monkeypatch, tree_impl) -> tuple[dict, str]:
    """Run the real agent with GCS/Firestore stubbed and the tree call injected.

    Returns (what the stubs recorded, the agent's emitted summary text).
    """
    seen: dict = {}

    def _gcs(file_name, payload):
        seen["gcs"] = file_name
        return json.dumps({"status": "success"})

    def _firestore(incident_id, payload):
        seen["firestore"] = incident_id
        return json.dumps({"status": "success"})

    def _verification(incident_id, payload):
        seen["verification"] = incident_id
        return json.dumps({"status": "success"})

    monkeypatch.setattr(persistence, "write_incident_file", _gcs)
    monkeypatch.setattr(persistence, "write_incident_to_firestore", _firestore)
    monkeypatch.setattr(persistence, "update_verification_scores", _verification)
    monkeypatch.setattr(persistence, "record_incident_contributions", tree_impl)

    async def _collect():
        return [
            event
            async for event in persistence.persistence_agent._run_async_impl(_Ctx(_state()))
        ]

    events = asyncio.run(_collect())
    assert len(events) == 1, "persistence emits exactly one event"
    text = "".join(part.text or "" for part in events[0].content.parts)
    return seen, text


class TestTreeWriteCannotLoseTheIncident:
    def test_incident_lands_even_when_the_tree_write_raises(self, monkeypatch):
        """A raise must be caught and reported, not propagated."""
        def _boom(category, incident_json):
            raise RuntimeError("Firestore transaction aborted")

        seen, text = _drive(monkeypatch, _boom)

        assert seen.get("gcs"), "GCS write must still have happened"
        assert seen.get("firestore") == seen.get("verification")
        assert seen["firestore"].endswith("-sql-database")
        assert "RuntimeError" in text
        assert "Firestore transaction aborted" in text

        # `_run_async_impl` has an OUTER except around the whole body, so with
        # the inner guard deleted the summary still says "non-fatal". What
        # changes is which subsystem it blames -- assert on that, not the word.
        assert "Decision tree error (non-fatal)" in text
        assert "Persistence error" not in text, (
            "the raise escaped to the outer bookkeeping handler, so a "
            "decision-tree defect is being reported as a persistence failure"
        )

    def test_a_returned_error_string_is_surfaced_not_swallowed(self, monkeypatch):
        """The tool catches its own errors, so this is not an exception. It must
        still reach the summary, or a broken learning loop looks healthy."""
        def _errored(category, incident_json):
            return json.dumps({"error": "record_incident_contributions failed: 403"})

        seen, text = _drive(monkeypatch, _errored)

        assert seen.get("firestore")
        assert '"error"' in text
        assert "403" in text

    def test_the_category_and_payload_handed_to_the_tree_are_the_persisted_ones(
        self, monkeypatch
    ):
        """Keyed on the category the incident was filed under, or hit rates
        accrue to the wrong tree."""
        captured: dict = {}

        def _capture(category, incident_json):
            captured["category"] = category
            captured["incident"] = json.loads(incident_json)
            return json.dumps({"status": "success", "version": 2})

        seen, _ = _drive(monkeypatch, _capture)

        assert captured["category"] == "sql-database"
        assert captured["incident"]["incident_id"] == seen["firestore"]
        assert captured["incident"]["findings"]["findings_datadog"]

    def test_the_rig_can_observe_a_success(self, monkeypatch):
        """Control: without it the tests above could pass because the harness
        never reaches the tree call."""
        def _ok(category, incident_json):
            return json.dumps({"status": "success", "version": 2})

        _, text = _drive(monkeypatch, _ok)

        assert '"status": "success"' in text
        assert "non-fatal" not in text

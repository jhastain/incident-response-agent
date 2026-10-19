"""The PagerDuty note must carry the verification verdict (ADO-200008).

The defect this locks down: `agent_client.investigate()` drains the whole SSE stream
before returning, so `result["verification"]` — the verifier's tier — is already in hand
when the note is composed. `main.py` used to read only `result["report"]` and throw the
verdict away, so an on-call engineer read an UNRELIABLE triage that looked identical to a
verified one.

The tier goes in the note WRAPPER, not the report body: it is delivery metadata about how
far the agent trusts its own write-up, and ADO-200018 is concurrently cutting the report
to four sections.

Two properties matter more than the exact wording:
  - MEDIUM must not read as a failure. It is the ceiling the verifier can award (HIGH
    requires a human) and the modal outcome.
  - A missing verdict (~22% of runs, ADO-200015) must say so, not imply a pass.
"""

from __future__ import annotations

import json

import pytest

from integrations.pagerduty.main import (
    _build_note,
    _confidence_banner,
    _parse_verification,
    _verification_tier,
)


def _result(verification, report: str = "## TL;DR\nCloud Run rollout 00123 OOM-killed.") -> dict:
    return {"ok": True, "error": None, "report": report, "verification": verification}


def _payload(tier: str) -> str:
    return json.dumps(
        {
            "diagnosis": "STRONG",
            "solution": "ACTIONABLE",
            "efficiency": "ADEQUATE",
            "automated_tier": tier,
            "evidence_citations_valid": 8,
            "evidence_citations_invalid": 1,
        }
    )


class TestTierExtraction:
    @pytest.mark.parametrize("tier", ["MEDIUM", "LOW", "UNRELIABLE"])
    def test_plain_json(self, tier):
        assert _verification_tier(_payload(tier)) == tier

    def test_fenced_json(self):
        # The verifier is an LLM and routinely fences its output.
        raw = "```json\n" + _payload("UNRELIABLE") + "\n```"
        assert _verification_tier(raw) == "UNRELIABLE"

    def test_bare_fence_without_language(self):
        raw = "```\n" + _payload("LOW") + "\n```"
        assert _verification_tier(raw) == "LOW"

    def test_json_wrapped_in_prose(self):
        raw = "Here is my assessment:\n" + _payload("MEDIUM") + "\nLet me know if you need more."
        assert _verification_tier(raw) == "MEDIUM"

    def test_already_a_dict(self):
        assert _verification_tier({"automated_tier": "LOW"}) == "LOW"

    def test_lowercase_is_normalized(self):
        assert _verification_tier('{"automated_tier": "unreliable"}') == "UNRELIABLE"

    def test_surrounding_whitespace_is_stripped(self):
        assert _verification_tier('{"automated_tier": "  MEDIUM  "}') == "MEDIUM"

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "the model returned no JSON at all",
            "{not valid json",
            "[]",
            json.dumps({"diagnosis": "STRONG"}),  # scored, but no tier field
            json.dumps({"automated_tier": None}),
            json.dumps({"automated_tier": 3}),
            json.dumps({"automated_tier": ""}),
        ],
    )
    def test_absent_or_unparseable_yields_empty(self, raw):
        assert _verification_tier(raw) == ""

    def test_parse_never_raises_on_junk(self):
        assert _parse_verification(object()) == {}
        assert _parse_verification(12345) == {}


class TestBanners:
    def test_medium_does_not_read_as_a_failure(self):
        banner = _confidence_banner(_payload("MEDIUM"))
        assert "MEDIUM" in banner
        # MEDIUM is the modal outcome (23 of 35 scored runs) and the ceiling the verifier
        # can award. It must not carry a warning marker.
        assert "⚠️" not in banner and "🛑" not in banner
        assert "HIGH requires a human" in banner

    def test_low_warns_and_asks_for_confirmation(self):
        banner = _confidence_banner(_payload("LOW"))
        assert "LOW" in banner
        assert "⚠️" in banner
        assert "Confirm" in banner

    def test_unreliable_says_the_agent_does_not_trust_itself(self):
        banner = _confidence_banner(_payload("UNRELIABLE"))
        assert "UNRELIABLE" in banner
        assert "🛑" in banner
        assert "could NOT verify" in banner
        assert "unconfirmed" in banner

    def test_missing_verdict_states_absence_without_implying_a_pass(self):
        banner = _confidence_banner(None)
        assert "not reported" in banner
        # Must not claim any tier the run did not produce.
        for tier in ("MEDIUM", "LOW", "UNRELIABLE"):
            assert tier not in banner
        assert "No warning here is not a pass." in banner

    def test_unknown_tier_is_reported_verbatim_not_mapped(self):
        # HIGH is explicitly forbidden to the verifier; if it ever appears, say so rather
        # than silently treating it as a pass.
        banner = _confidence_banner(_payload("HIGH"))
        assert "HIGH" in banner
        assert "does not recognize" in banner
        assert "unverified" in banner

    def test_tier_value_is_sanitized_before_it_lands_in_the_note(self):
        banner = _confidence_banner('{"automated_tier": "MEDIUM <script>x</script>"}')
        assert "<script>" not in banner


class TestNoteComposition:
    def test_header_is_first_line_and_unchanged(self):
        note = _build_note(_result(_payload("MEDIUM")))
        assert note.splitlines()[0] == "🤖 **Incident Response Agent — Triage**"

    def test_banner_sits_between_header_and_report(self):
        note = _build_note(_result(_payload("UNRELIABLE")))
        lines = note.splitlines()
        assert "UNRELIABLE" in lines[1]
        assert lines[2] == ""
        assert note.endswith("Cloud Run rollout 00123 OOM-killed.")

    @pytest.mark.parametrize("tier", ["MEDIUM", "LOW", "UNRELIABLE"])
    def test_report_body_is_passed_through_untouched(self, tier):
        report = "## TL;DR\nA\n\n## Incident Summary\nB\n\n## What Happened\nC"
        note = _build_note(_result(_payload(tier), report=report))
        assert note.endswith(report)

    def test_missing_verification_still_produces_a_full_note(self):
        note = _build_note(_result(""))
        assert note.startswith("🤖 **Incident Response Agent — Triage**")
        assert "not reported" in note
        assert note.endswith("Cloud Run rollout 00123 OOM-killed.")

    def test_verification_key_entirely_absent_does_not_crash(self):
        note = _build_note({"ok": True, "error": None, "report": "body"})
        assert "not reported" in note
        assert note.endswith("body")

    def test_failure_note_is_unchanged_and_carries_no_banner(self):
        note = _build_note({"ok": False, "error": "boom", "report": "", "verification": ""})
        assert note == (
            "🤖 Incident Response Agent — triage unavailable.\nError: boom"
        )
        assert "confidence" not in note.lower()

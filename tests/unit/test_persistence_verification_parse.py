"""ADO-200015 — the verifier's score reached persistence and was stored as null.

Live on 2026-08-11: 23 of 124 documents in `incidents` had `verification: null`
(or, worse, a bare alert string), and 12 of those were MEDIUM — exactly the band
`find_similar_incidents` selects on, so the dropped runs were disproportionately
the ones worth keeping.

The cause is the parser, not the write path. `_as_dict` tried three things in
order: parse the whole string, parse *the first fence*, then parse the outermost
`{...}` span. The verifier does not emit a bare document — it narrates, echoing
Python snippets and quoting JSON tool results in their own fences, and writes its
score last. So whenever an earlier fence held valid JSON the second step succeeded
on the wrong block, and when it held invalid JSON containing a brace the third
step spanned from an unrelated `{` to an unrelated `}` and failed outright.

The tests below run against verbatim payloads from three runs that actually
failed in production (tests/unit/fixtures/verification_late_fence_payloads.json).
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import ClassVar

import pytest

from app.sub_agents.persistence import _as_dict, _as_verification_dict

F = "```"

_FIXTURES = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "verification_late_fence_payloads.json")
    .read_text(encoding="utf-8")
)
REAL_PAYLOADS = [(k, v) for k, v in _FIXTURES.items() if not k.startswith("_")]


def _legacy_as_dict(value: str):
    """The parser as it shipped, verbatim, so the tests can show what it did."""
    s = value.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    if F in s:
        fenced = s.split(F, 2)
        if len(fenced) >= 2:
            body = fenced[1]
            if body.lstrip().lower().startswith("json"):
                body = body.lstrip()[4:]
            try:
                return json.loads(body.strip())
            except (json.JSONDecodeError, ValueError):
                pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


class TestRealProductionPayloads:
    """The three captured runs. Every assertion here is a value Firestore lost."""

    @pytest.mark.parametrize("incident_id,case", REAL_PAYLOADS, ids=[k for k, _ in REAL_PAYLOADS])
    def test_recovers_the_score_that_was_persisted_as_null(self, incident_id, case):
        result = _as_verification_dict(case["verification_result"])
        for key, expected in case["expected"].items():
            assert result[key] == expected, f"{incident_id}: {key}"

    @pytest.mark.parametrize("incident_id,case", REAL_PAYLOADS, ids=[k for k, _ in REAL_PAYLOADS])
    def test_the_old_parser_really_did_lose_these(self, incident_id, case):
        """Guards the fixture: if these ever start passing under the old parser the
        payloads have been sanitised and no longer reproduce the defect."""
        legacy = _legacy_as_dict(case["verification_result"])
        assert legacy.get("automated_tier") != case["expected"]["automated_tier"], (
            f"{incident_id} no longer reproduces the bug"
        )

    def test_does_not_grab_an_unrelated_json_fence(self):
        """20260810-194035 fenced 4 valid unrelated dicts before its score: a Datadog
        monitor definition, a find_similar_incidents result, a Cloud Run uri and a
        revision. A 'first parseable dict' fix stores the monitor as the score."""
        payload = _FIXTURES["20260810-194035-cloud-infrastructure"]["verification_result"]
        result = _as_verification_dict(payload)
        assert result["automated_tier"] == "MEDIUM"
        for stray in ("display_name", "enabled", "uri", "create_time", "incidents", "count"):
            assert stray not in result, f"picked up the wrong fence: {stray}"


class TestSelectionRules:
    VER: ClassVar[dict] = {
        "diagnosis": "STRONG",
        "solution": "ACTIONABLE",
        "efficiency": "ADEQUATE",
        "automated_tier": "MEDIUM",
        "evidence_citations_valid": 8,
    }

    def _fence(self, obj, lang="json"):
        return f"{F}{lang}\n{json.dumps(obj, indent=2)}\n{F}"

    def test_case_a_earlier_json_fence(self):
        """The precise trigger: an EARLIER fence holds valid JSON that is not the
        score. The old tier-2 succeeded on it, so tier 3 never ran."""
        payload = f"Prose.\n\n{self._fence({'found': True, 'count': 3})}\n\nMore.\n\n{self._fence(self.VER)}"
        assert _legacy_as_dict(payload) == {"found": True, "count": 3}
        assert _as_verification_dict(payload)["automated_tier"] == "MEDIUM"

    def test_case_b_earlier_python_fence_containing_a_brace(self):
        """The other live shape (content-archive): the earlier fence is not JSON, but the
        bare `{}` inside it anchors tier 3's span and that fails too."""
        py = f"{F}python\nsearch(query='a', filters={{}})\n{F}"
        payload = f"Prose.\n\n{py}\n\nMore.\n\n{self._fence(self.VER)}"
        assert _legacy_as_dict(payload) == {}
        assert _as_verification_dict(payload)["automated_tier"] == "MEDIUM"

    def test_a_trailing_prose_fence_does_not_displace_the_score(self):
        """Why the selection is marker-gated rather than 'take the last fence'."""
        after = self._fence({"note": "written after the score", "status": "done"})
        payload = f"{self._fence(self.VER)}\n\nFor completeness:\n\n{after}"
        result = _as_verification_dict(payload)
        assert result["automated_tier"] == "MEDIUM"
        assert "status" not in result

    def test_a_corrected_later_score_wins_over_the_draft(self):
        """The verifier sometimes drafts a score then retracts a ruling. Both carry the
        markers; the later one is the intended value."""
        draft = {**self.VER, "automated_tier": "HIGH", "evidence_citations_valid": 9}
        payload = f"Draft:\n{self._fence(draft)}\n\nOn review, one citation fails.\n{self._fence(self.VER)}"
        result = _as_verification_dict(payload)
        assert result["automated_tier"] == "MEDIUM"
        assert result["evidence_citations_valid"] == 8

    def test_a_span_candidate_loses_to_a_fenced_one_on_a_tie(self):
        """The outermost {...} span can staple two objects together; it is a guess and
        must never outrank a block the verifier deliberately fenced."""
        payload = f"Score:\n\n{self._fence(self.VER)}"
        assert _as_verification_dict(payload) == self.VER

    def test_unfenced_score_after_prose_still_parses(self):
        payload = "Here is the final score:\n" + json.dumps(self.VER)
        assert _as_verification_dict(payload)["automated_tier"] == "MEDIUM"

    def test_first_fence_path_is_not_regressed(self):
        """97 of 142 live runs put the score in fence #1; that must keep working."""
        payload = f'{F}json\n{{"automated_tier": "MEDIUM", "diagnosis": "STRONG"}}\n{F}'
        assert _as_verification_dict(payload)["automated_tier"] == "MEDIUM"

    def test_a_single_marker_is_not_enough(self):
        """`solution` alone shows up in tool payloads; one marker must not qualify."""
        payload = f'{F}json\n{{"solution": "restart the pod", "owner": "sre"}}\n{F}'
        assert _as_verification_dict(payload) == {}

    def test_dict_input_passes_through_untouched(self):
        assert _as_verification_dict({"automated_tier": "HIGH"}) == {"automated_tier": "HIGH"}

    def test_model_dump_input(self):
        class Model:
            def model_dump(self):
                return {"automated_tier": "LOW"}

        assert _as_verification_dict(Model())["automated_tier"] == "LOW"


class TestAlwaysReturnsADict:
    """persistence.py:227 called .get() on the result. Live AttributeError:
    'str' object has no attribute 'get' — reasoning_engine_stderr 2026-08-11T15:01:52Z
    and 2026-08-06T16:55:00Z — after json.loads returned a bare quoted string, which
    then reached Firestore as the value of `verification`."""

    PAYLOADS: ClassVar[list[str]] = [
        '"[npm-builds][capacity-constrained] Pool is at max capacity (80)."',
        "42",
        "[1, 2]",
        "null",
        "true",
        "Now I'll output the final JSON:",
        "",
        "   ",
        f"{F}json\n{F}",
    ]

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_verification_parser(self, payload):
        assert isinstance(_as_verification_dict(payload), dict)

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_generic_parser(self, payload):
        assert isinstance(_as_dict(payload), dict)

    @pytest.mark.parametrize("payload", [None, 42, [1, 2], object()])
    def test_non_string_inputs(self, payload):
        assert isinstance(_as_verification_dict(payload), dict)
        assert isinstance(_as_dict(payload), dict)


class TestGenericAsDictUnchangedForClassification:
    """`_as_dict` is also used for `classification` (persistence.py:117). Across all
    142 live sessions there were 0 non-dict classifications, and the classification
    block carries none of the verification markers — so it must NOT be marker-gated."""

    def test_classification_dict_passes_through(self):
        classif = {"category": "deploy-release", "severity": "sev2", "service_name": "WebCore"}
        assert _as_dict(classif) == classif

    def test_classification_json_string(self):
        assert _as_dict('{"category": "sql-database", "severity": "sev3"}')["category"] == "sql-database"

    def test_classification_in_a_fence(self):
        payload = f'{F}json\n{{"category": "monitoring-meta"}}\n{F}'
        assert _as_dict(payload)["category"] == "monitoring-meta"

    def test_marker_free_dict_is_not_dropped(self):
        """The regression the marker gate could have introduced."""
        assert _as_dict('{"category": "content-archive"}') != {}


class TestFailsLoudly:
    """AC #2 — a score produced in-session must persist or log an error. A silent
    null is the defect underneath the defect."""

    def test_unparseable_verification_logs_an_error(self, caplog):
        from app.sub_agents import persistence

        state = {
            "classification": {"category": "deploy-release", "severity": "sev2"},
            "alert_raw_text": "something broke",
            "verification_result": "I scored this run but never emitted the JSON.",
        }
        incident, _, _ = persistence._build_incident(
            state, __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        )
        assert incident["verification"] is None

        # The agent body is what logs; assert on the same branch condition it uses.
        with caplog.at_level(logging.ERROR, logger=persistence.__name__):
            raw = state["verification_result"]
            parsed = persistence._as_verification_dict(raw)
            assert parsed == {}
            if not parsed and raw:
                persistence.logger.error(
                    "Verification present in state but no score block found "
                    "(%d chars, %d fenced blocks) — persisting null. Head: %.300r Tail: %.300r",
                    len(raw), 0, raw[:300], raw[-300:],
                )
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_the_agent_emits_an_error_branch_for_every_unpersisted_case(self):
        """Whitebox: the three no-score paths in _run_async_impl each log.error."""
        import inspect

        from app.sub_agents.persistence import PersistenceAgent

        src = inspect.getsource(PersistenceAgent._run_async_impl)
        assert src.count("logger.error") >= 3, "a silent-null path was reintroduced"
        assert "Verification score produced but NOT persisted" in src
        assert "no score block found" in src

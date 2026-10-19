"""Tests for the decision-tree feedback loop.

The Firestore tests use a hand-rolled fake so the real `firestore.transactional`
decorator still runs the read-modify-write logic. Patching the decorator away
would leave nothing under test.
"""

from __future__ import annotations

import json

import pytest

from app.tools.decision_tree_learning import (
    AMBIENT_MAX,
    HIT_RATE_CEILING,
    HIT_RATE_FLOOR,
    PRIOR,
    apply_contributions,
    artifact_tokens,
    evaluate_skip_rule,
    is_incident_usable,
    posterior_hit_rate,
    score_contributions,
)


def _incident(findings: dict, report: str = "", synthesis: str = "", **extra) -> dict:
    base = {
        "incident_id": "20260820-120000-test",
        "category": "sql-database",
        "findings": findings,
        "report": report,
        "synthesis": synthesis,
        "verification": {"diagnosis": "STRONG", "automated_tier": "MEDIUM"},
    }
    base.update(extra)
    return base


class TestArtifactTokens:
    def test_extracts_backticks_urls_and_ids(self):
        text = (
            "Revision `WebCore-prod-run-apiv2-1000005-rc` failed. "
            "See https://acme.sentry.io/issues/WEBCORE-DOTNET-5E6F and "
            "ADO build 2313076, task ServerTasks-1000001."
        )
        tokens = artifact_tokens(text)
        assert "WebCore-prod-run-apiv2-1000005-rc" in tokens
        assert "https://acme.sentry.io/issues/WEBCORE-DOTNET-5E6F" in tokens
        assert "WEBCORE-DOTNET-5E6F" in tokens
        assert "2313076" in tokens
        assert "ServerTasks-1000001" in tokens

    def test_drops_tool_names(self):
        # These were the only "shared artifact" for many known_issues findings:
        # the agent narrating its plumbing, quoted back by synthesis.
        tokens = artifact_tokens("I called `search_issues` and `wit_get_query_results_by_id()`.")
        assert tokens == set()

    def test_drops_short_and_stopword_spans(self):
        assert artifact_tokens("`id` `true` `200`") == set()

    def test_prose_alone_yields_nothing(self):
        assert artifact_tokens("The database was slow and the deploy looked suspicious.") == set()

    def test_non_string_is_empty(self):
        assert artifact_tokens(None) == set()
        assert artifact_tokens(42) == set()


class TestScoreContributions:
    def test_agent_whose_artifact_reaches_the_report_contributed(self):
        incident = _incident(
            findings={"findings_sentry": "Found issue `WEBCORE-DOTNET-5E6F` with 40499 events."},
            report="Mechanism: Redis timeout, per `WEBCORE-DOTNET-5E6F`.",
        )
        assert score_contributions(incident) == {"sentry_agent": True}

    def test_long_no_data_block_does_not_contribute(self):
        # The case that kills the "non-empty" rule: long, honest, unused.
        incident = _incident(
            findings={"findings_known_issues": "ADO keyword search is not available. " * 100},
            report="Mechanism: Redis timeout, per `WEBCORE-DOTNET-5E6F`.",
        )
        assert score_contributions(incident) == {"known_issues_agent": False}

    def test_restating_an_input_does_not_contribute(self):
        incident = _incident(
            findings={"findings_teams": "Discussion about `WebCore-prod-run-apiv2-us-c1`."},
            report="The service `WebCore-prod-run-apiv2-us-c1` is degraded.",
            service_context="Service `WebCore-prod-run-apiv2-us-c1` runs the API.",
        )
        assert score_contributions(incident) == {"teams_context_agent": False}

    def test_evidence_used_only_by_synthesis_still_contributes(self):
        # The report drops MODERATE findings, so evidence can be decisive in
        # cross-validation and never reach Key Findings.
        incident = _incident(
            findings={"findings_gcp_metrics": "CPU flat, revision `1000005-rc` unremarkable."},
            report="Mechanism: unrelated.",
            synthesis="Check 2 rules out CPU: revision `1000005-rc` shows flat CPU.",
        )
        assert score_contributions(incident)["gcp_metrics_agent"] is True

    def test_absent_findings_key_is_omitted_not_scored_as_a_miss(self):
        # persistence.py omits the key when state has nothing for it.
        incident = _incident(
            findings={"findings_sentry": "Issue `WEBCORE-DOTNET-5E6F`."},
            report="Per `WEBCORE-DOTNET-5E6F`.",
        )
        scored = score_contributions(incident)
        assert "datadog_agent" not in scored
        assert set(scored) == {"sentry_agent"}

    def test_blank_findings_value_is_omitted(self):
        incident = _incident(
            findings={"findings_sentry": "   ", "findings_datadog": "Monitor `10000001` alerting."},
            report="Monitor `10000001` fired.",
        )
        assert set(score_contributions(incident)) == {"datadog_agent"}

    def test_ambient_token_shared_by_too_many_agents_credits_nobody(self):
        shared = "`WebCore-prod-run-apiv2-1000005-rc`"
        keys = [
            "findings_sentry", "findings_datadog", "findings_gcp_logging",
            "findings_cloud_run", "findings_deploy", "findings_teams",
        ][: AMBIENT_MAX + 1]
        incident = _incident(
            findings=dict.fromkeys(keys, f"Saw {shared}."),
            report=f"Revision {shared} is implicated.",
        )
        assert not any(score_contributions(incident).values())

    def test_corroboration_by_two_agents_credits_both(self):
        # INVESTIGATION_RULES #2 requires two sources, so convergence counts
        # for both.
        shared = "`WebCore-prod-run-apiv2-1000005-rc`"
        incident = _incident(
            findings={
                "findings_sentry": f"Saw {shared}.",
                "findings_cloud_run": f"Also saw {shared}.",
            },
            report=f"Revision {shared} is implicated.",
        )
        assert score_contributions(incident) == {
            "sentry_agent": True, "cloud_run_health_agent": True
        }

    def test_no_findings_map_scores_nothing(self):
        assert score_contributions({"findings": None}) == {}
        assert score_contributions({}) == {}


class TestExclusionReasonsAreDiscriminating:
    """Every reason `is_incident_usable` returns must be distinguishable.

    The miner buckets exclusions by this string and prints them as the
    `excluded: {...}` line under each category table -- that is how a reader of a
    THIN category tells "predates the findings map" from "the verifier rejected
    it", two facts with opposite remedies.

    The set comparison catches what the per-case checks cannot: they run
    independently, so two causes can converge on one string that satisfies both.
    """

    def _cases(self) -> dict:
        no_findings = _incident(findings={"findings_sentry": "x"}, report="y")
        no_findings["findings"] = None

        no_downstream = _incident(findings={"findings_sentry": "x"}, report="", synthesis="")

        unverified = _incident(findings={"findings_sentry": "x"}, report="y")
        unverified["verification"] = None

        no_verdict = _incident(findings={"findings_sentry": "x"}, report="y")
        no_verdict["verification"] = {"reasoning": {"diagnosis_notes": "..."}}

        unsupported = _incident(findings={"findings_sentry": "x"}, report="y")
        unsupported["verification"] = {"diagnosis": "UNSUPPORTED", "automated_tier": "LOW"}

        unreliable = _incident(findings={"findings_sentry": "x"}, report="y")
        unreliable["verification"] = {"diagnosis": "PARTIAL", "automated_tier": "UNRELIABLE"}

        return {
            "no findings map": (no_findings, False, False),
            "no downstream text": (no_downstream, False, False),
            "unverified, required": (unverified, True, False),
            "verification with no verdict": (no_verdict, True, False),
            "UNSUPPORTED diagnosis": (unsupported, False, False),
            "UNRELIABLE tier": (unreliable, False, False),
        }

    def test_every_exclusion_cause_states_a_DISTINCT_reason(self):
        reasons = {}
        for label, (incident, require_verified, _) in self._cases().items():
            usable, reason = is_incident_usable(incident, require_verified=require_verified)
            assert usable is False, f"{label} should be excluded"
            assert reason and reason.strip(), f"{label} returned an empty reason"
            reasons[label] = reason

        # A set: per-case assertions run independently and cannot catch two
        # causes collapsing onto one string.
        assert len(set(reasons.values())) == len(reasons), (
            f"exclusion reasons are not distinguishable: {reasons}"
        )

    def test_each_reason_names_its_own_cause(self):
        """Distinctness alone is satisfiable by 'reason-1'..'reason-6'. Each string
        must also carry the token an operator would grep the miner output for."""
        cases = self._cases()
        expected = {
            "no findings map": "findings",
            "no downstream text": "synthesis",
            "unverified, required": "verification",
            "verification with no verdict": "verdict",
            "UNSUPPORTED diagnosis": "UNSUPPORTED",
            "UNRELIABLE tier": "UNRELIABLE",
        }
        for label, token in expected.items():
            incident, require_verified, _ = cases[label]
            _, reason = is_incident_usable(incident, require_verified=require_verified)
            assert token.lower() in reason.lower(), (
                f"{label}: reason {reason!r} does not name its cause ({token})"
            )

    def test_the_usable_reasons_are_also_distinguishable(self):
        """A counted incident reports WHY it was counted; the miner does not bucket
        these, but an unverified run and a scored one must not read alike."""
        unverified = _incident(findings={"findings_sentry": "x"}, report="y")
        unverified["verification"] = None
        scored = _incident(findings={"findings_sentry": "x"}, report="y")

        ok_unverified, why_unverified = is_incident_usable(unverified)
        ok_scored, why_scored = is_incident_usable(scored)

        assert ok_unverified and ok_scored
        assert why_unverified != why_scored
        assert "unverified" in why_unverified.lower()


class TestIsIncidentUsable:
    def test_unsupported_diagnosis_is_excluded(self):
        incident = _incident(findings={"findings_sentry": "x"}, report="y")
        incident["verification"] = {"diagnosis": "UNSUPPORTED", "automated_tier": "LOW"}
        usable, reason = is_incident_usable(incident)
        assert usable is False
        assert "UNSUPPORTED" in reason

    def test_unreliable_tier_is_excluded(self):
        incident = _incident(findings={"findings_sentry": "x"}, report="y")
        incident["verification"] = {"diagnosis": "PARTIAL", "automated_tier": "UNRELIABLE"}
        assert is_incident_usable(incident)[0] is False

    def test_unverified_is_included_by_default_and_excluded_on_demand(self):
        incident = _incident(findings={"findings_sentry": "x"}, report="y")
        incident["verification"] = None
        assert is_incident_usable(incident)[0] is True
        assert is_incident_usable(incident, require_verified=True)[0] is False

    def test_no_downstream_text_is_excluded(self):
        incident = _incident(findings={"findings_sentry": "x"}, report="", synthesis="")
        usable, reason = is_incident_usable(incident)
        assert usable is False
        assert "synthesis or report" in reason

    def test_no_findings_map_is_excluded(self):
        assert is_incident_usable({"report": "x", "findings": None})[0] is False


class TestPosteriorHitRate:
    def test_zero_samples_is_the_prior(self):
        # No discontinuity at rollout: matches what every live tree shows.
        assert posterior_hit_rate(0, 0) == PRIOR

    def test_single_incident_moves_less_than_five_points(self):
        assert posterior_hit_rate(1, 1) == pytest.approx(6 / 11, abs=1e-4)
        assert posterior_hit_rate(0, 1) == pytest.approx(5 / 11, abs=1e-4)
        assert abs(posterior_hit_rate(1, 1) - PRIOR) < 0.05
        assert abs(posterior_hit_rate(0, 1) - PRIOR) < 0.05

    def test_perfect_run_never_reaches_one(self):
        assert posterior_hit_rate(1000, 1000) <= HIT_RATE_CEILING
        assert posterior_hit_rate(1000, 1000) < 1.0

    def test_total_failure_never_reaches_zero_and_respects_the_floor(self):
        # Stops an early run of bad luck locking an agent out.
        assert posterior_hit_rate(0, 1000) >= HIT_RATE_FLOOR
        assert posterior_hit_rate(0, 1000) > 0.0

    def test_recovers_from_an_early_bad_streak(self):
        after_bad_streak = posterior_hit_rate(0, 10)
        recovered = posterior_hit_rate(30, 40)
        assert after_bad_streak < PRIOR < recovered

    def test_clamps_nonsense_input(self):
        assert posterior_hit_rate(-5, -5) == PRIOR
        # hits can never exceed samples.
        assert posterior_hit_rate(100, 3) == posterior_hit_rate(3, 3)


def _tree(**overrides) -> dict:
    tree = {
        "category": "sql-database",
        "version": 1,
        "incident_count": 0,
        "skip_rules": [],
        "triage_steps": [
            {"order": 1, "agent": "datadog_agent", "action": "a", "tier": 1, "hit_rate": 0.5},
            {"order": 2, "agent": "teams_context_agent", "action": "b", "tier": 1, "hit_rate": 0.5},
            {"order": 3, "agent": "sql_dba_agent", "action": "c", "tier": 2, "hit_rate": 0.5},
        ],
    }
    tree.update(overrides)
    return tree


class TestApplyContributions:
    def test_updates_hit_rate_and_counters(self):
        new_tree, changes = apply_contributions(_tree(), {"datadog_agent": (8, 10)}, 10)
        step = new_tree["triage_steps"][0]
        assert step["hit_count"] == 8
        assert step["sample_count"] == 10
        assert step["hit_rate"] == posterior_hit_rate(8, 10)
        assert new_tree["incident_count"] == 10
        assert any("datadog_agent" in c for c in changes)

    def test_does_not_mutate_the_input_tree(self):
        original = _tree()
        apply_contributions(original, {"datadog_agent": (8, 10)}, 10)
        assert original["triage_steps"][0]["hit_rate"] == 0.5
        assert original["incident_count"] == 0

    def test_counters_accumulate_across_passes(self):
        tree, _ = apply_contributions(_tree(), {"datadog_agent": (1, 1)}, 1)
        tree, _ = apply_contributions(tree, {"datadog_agent": (1, 1)}, 1)
        step = tree["triage_steps"][0]
        assert (step["hit_count"], step["sample_count"]) == (2, 2)
        assert step["hit_rate"] == posterior_hit_rate(2, 2)

    def test_tier2_step_is_never_scored(self):
        # tier2_findings is one flat string with no per-agent split.
        new_tree, _ = apply_contributions(_tree(), {"datadog_agent": (5, 5)}, 5)
        sql_dba = [s for s in new_tree["triage_steps"] if s["agent"] == "sql_dba_agent"][0]
        assert sql_dba["hit_rate"] == 0.5
        assert "sample_count" not in sql_dba

    def test_pinned_tree_is_untouched(self):
        pinned = _tree(optimization_pinned=True)
        new_tree, changes = apply_contributions(pinned, {"datadog_agent": (9, 10)}, 10)
        assert changes == []
        assert new_tree["triage_steps"][0]["hit_rate"] == 0.5
        assert new_tree["incident_count"] == 0

    def test_human_locked_step_keeps_its_hit_rate_but_still_counts(self):
        # Counters keep moving so a reviewer can see what the automatic path
        # would have said.
        tree = _tree()
        tree["triage_steps"][1].update({
            "hit_rate": 0.2, "human_locked": True, "human_locked_by": "first.last",
        })
        new_tree, changes = apply_contributions(tree, {"teams_context_agent": (10, 10)}, 10)
        locked = new_tree["triage_steps"][1]
        assert locked["hit_rate"] == 0.2
        assert locked["sample_count"] == 10
        assert locked["hit_count"] == 10
        assert any("held at 0.2" in c and "first.last" in c for c in changes)

    def test_a_repeated_agent_is_skipped_rather_than_flattened(self):
        # content-archive lists one agent five times with hand-set rates; a
        # per-agent signal would write one number to all of them.
        tree = _tree(triage_steps=[
            {"order": 1, "agent": "deploy_correlation_agent", "tier": 1, "hit_rate": 0.9},
            {"order": 2, "agent": "deploy_correlation_agent", "tier": 1, "hit_rate": 0.7},
            {"order": 3, "agent": "datadog_agent", "tier": 1, "hit_rate": 0.6},
        ])
        new_tree, changes = apply_contributions(
            tree, {"deploy_correlation_agent": (1, 6), "datadog_agent": (6, 6)}, 6
        )
        assert [s["hit_rate"] for s in new_tree["triage_steps"][:2]] == [0.9, 0.7]
        assert new_tree["triage_steps"][2]["hit_rate"] == posterior_hit_rate(6, 6)
        assert any("SKIPPED" in c and "deploy_correlation_agent" in c for c in changes)

    def test_agent_with_no_observation_is_left_alone(self):
        new_tree, _ = apply_contributions(_tree(), {"datadog_agent": (1, 1)}, 1)
        teams = new_tree["triage_steps"][1]
        assert teams["hit_rate"] == 0.5
        assert "sample_count" not in teams


class TestEvaluateSkipRule:
    def test_equality_matches_case_insensitively(self):
        rule = {"field": "environment", "op": "==", "value": "Development"}
        assert evaluate_skip_rule(rule, {"environment": "development"}) is True
        assert evaluate_skip_rule(rule, {"environment": "production"}) is False

    def test_inequality_and_membership(self):
        assert evaluate_skip_rule(
            {"field": "severity", "op": "!=", "value": "sev1"}, {"severity": "sev3"}
        ) is True
        assert evaluate_skip_rule(
            {"field": "severity", "op": "in", "value": ["sev3", "sev4"]}, {"severity": "sev4"}
        ) is True
        assert evaluate_skip_rule(
            {"field": "severity", "op": "not_in", "value": ["sev3", "sev4"]}, {"severity": "sev1"}
        ) is True

    def test_unknown_field_or_operator_fails_open(self):
        # A malformed rule must run the step, not skip it: a skipped step is
        # invisible in the report, a redundant one only costs tokens.
        assert evaluate_skip_rule({"field": "nope", "op": "==", "value": "x"}, {}) is False
        assert evaluate_skip_rule(
            {"field": "environment", "op": "=~", "value": "dev"}, {"environment": "dev"}
        ) is False

    def test_never_evaluates_a_string_expression(self):
        # The old design eval()d stored source, so any Firestore write was code
        # execution here. The expression form must be inert.
        payload = {"condition": "__import__('os').system('echo pwned')"}
        assert evaluate_skip_rule(payload, {"environment": "development"}) is False


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def get(self, transaction=None):
        return _FakeSnapshot(self._store.get(self._name))


class _FakeTransaction:
    """Enough of a Firestore transaction for the writers' read-modify-write.

    The real `_Transactional` wrapper runs, so this has to implement the private
    protocol it drives, per google-cloud-firestore 2.28.0 (uv.lock:1399-1400):
    `__call__` reads `_read_only` and `_max_attempts`; `_pre_commit` calls
    `_clean_up()`, `_begin(retry_id=...)`, reads `_id`, then `_commit()`; any
    exception triggers `_rollback()`.
    """

    _read_only = False
    _max_attempts = 1

    def __init__(self, store):
        self._store = store
        self.updates = []
        self._id = b"fake-txn"

    # -- the wrapper's protocol -------------------------------------------
    def _clean_up(self):
        self._id = None

    def _begin(self, retry_id=None):
        self._id = retry_id or b"fake-txn"

    def _commit(self):
        return []

    def _rollback(self):
        self._id = None

    @property
    def id(self):
        return self._id

    # -- the part the writers actually use --------------------------------
    def update(self, doc_ref, fields):
        self.updates.append(fields)
        self._store[doc_ref._name] = {**self._store.get(doc_ref._name, {}), **fields}


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, name):
        return _FakeDocRef(self._store, name)


class _FakeDb:
    def __init__(self, store):
        self._store = store

    def collection(self, _name):
        return _FakeCollection(self._store)

    def transaction(self):
        return _FakeTransaction(self._store)


@pytest.fixture()
def firestore_store(monkeypatch):
    """A single-document fake `decision_trees` collection wired into the writers."""
    import app.tools.firestore_tools as ft

    store = {"sql-database": _tree()}
    monkeypatch.setattr(ft, "_get_db", lambda: _FakeDb(store))
    return store


class TestRecordIncidentContributions:
    def _incident_json(self, incident_id="20260820-120000-sql-database"):
        return json.dumps(_incident(
            findings={
                "findings_datadog": "Monitor `10000001` shows blocking chain.",
                "findings_teams": "Nothing relevant in the channel.",
            },
            report="Mechanism: blocking chain, per monitor `10000001`.",
            incident_id=incident_id,
        ))

    def test_first_write_updates_and_versions(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        result = json.loads(record_incident_contributions("sql-database", self._incident_json()))
        assert result["status"] == "success"
        assert result["contributed"] == ["datadog_agent"]
        assert result["did_not_contribute"] == ["teams_context_agent"]

        tree = firestore_store["sql-database"]
        assert tree["version"] == 2                       # AC 6
        assert tree["last_optimized"] is not None         # AC 6
        assert tree["incident_count"] == 1
        assert len(tree["optimization_log"]) == 1         # AC 6
        assert tree["optimization_log"][0]["actor"] == "automatic"
        assert tree["optimization_log"][0]["incidents_analyzed"] == 1

    def test_replay_of_the_same_incident_is_a_no_op(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        record_incident_contributions("sql-database", self._incident_json())
        after_first = json.loads(json.dumps(firestore_store["sql-database"], default=str))

        result = json.loads(record_incident_contributions("sql-database", self._incident_json()))
        assert result["status"] == "already_applied"
        after_replay = json.loads(json.dumps(firestore_store["sql-database"], default=str))
        assert after_first == after_replay

    def test_a_different_incident_does_count(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        record_incident_contributions("sql-database", self._incident_json("inc-a"))
        record_incident_contributions("sql-database", self._incident_json("inc-b"))
        tree = firestore_store["sql-database"]
        assert tree["incident_count"] == 2
        assert tree["version"] == 3
        datadog = [s for s in tree["triage_steps"] if s["agent"] == "datadog_agent"][0]
        assert (datadog["hit_count"], datadog["sample_count"]) == (2, 2)

    def test_unsupported_diagnosis_is_skipped_without_writing(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        incident = _incident(
            findings={"findings_datadog": "Monitor `10000001`."},
            report="Per monitor `10000001`.",
        )
        incident["verification"] = {"diagnosis": "UNSUPPORTED", "automated_tier": "LOW"}
        result = json.loads(
            record_incident_contributions("sql-database", json.dumps(incident))
        )
        assert result["status"] == "skipped"
        assert firestore_store["sql-database"]["version"] == 1

    def test_pinned_tree_records_the_incident_but_changes_nothing(self, firestore_store):
        # The id is still recorded, so unpinning later does not replay history.
        from app.tools.firestore_tools import record_incident_contributions

        firestore_store["sql-database"]["optimization_pinned"] = True
        result = json.loads(record_incident_contributions("sql-database", self._incident_json()))
        assert result["status"] == "pinned"
        tree = firestore_store["sql-database"]
        assert tree["version"] == 1
        assert tree["triage_steps"][0]["hit_rate"] == 0.5
        assert "20260820-120000-sql-database" in tree["applied_incident_ids"]

    def test_human_locked_step_survives_an_automatic_pass(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        teams = firestore_store["sql-database"]["triage_steps"][1]
        teams.update({"hit_rate": 0.2, "human_locked": True, "human_locked_by": "first.last"})

        for i in range(5):
            record_incident_contributions("sql-database", self._incident_json(f"inc-{i}"))

        teams = firestore_store["sql-database"]["triage_steps"][1]
        assert teams["hit_rate"] == 0.2
        assert teams["human_locked_by"] == "first.last"
        assert teams["sample_count"] == 5          # still measurable
        assert firestore_store["sql-database"]["triage_steps"][0]["hit_rate"] != 0.5

    def test_missing_tree_is_not_created(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        result = json.loads(record_incident_contributions("no-such-category", self._incident_json()))
        assert result["status"] == "skipped"
        assert "no-such-category" not in firestore_store

    def test_incident_without_an_id_is_refused(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        incident = _incident(findings={"findings_datadog": "x"}, report="y")
        del incident["incident_id"]
        result = json.loads(record_incident_contributions("sql-database", json.dumps(incident)))
        assert "error" in result

    def test_malformed_json_returns_an_error_rather_than_raising(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        assert "error" in json.loads(record_incident_contributions("sql-database", "{not json"))


class TestApplyEngineerCorrection:
    def test_requires_actor_and_reason(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        result = json.loads(apply_engineer_correction(
            "sql-database", json.dumps({"reorder": ["teams_context_agent"]})
        ))
        assert "error" in result
        assert firestore_store["sql-database"]["version"] == 1

    def test_set_hit_rate_locks_the_step(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        result = json.loads(apply_engineer_correction("sql-database", json.dumps({
            "actor": "first.last",
            "reason": "Teams chatter produced a fabricated trigger",
            "set_hit_rate": {"agent": "teams_context_agent", "hit_rate": 0.2},
        })))
        assert result["status"] == "success"
        teams = [s for s in firestore_store["sql-database"]["triage_steps"]
                 if s["agent"] == "teams_context_agent"][0]
        assert teams["hit_rate"] == 0.2
        assert teams["human_locked"] is True
        assert teams["human_locked_by"] == "first.last"
        assert teams["human_locked_reason"] == "Teams chatter produced a fabricated trigger"

    def test_hand_set_rate_is_clamped_to_the_same_bounds(self, firestore_store):
        # 0.0 would let a consumer filtering on hit_rate drop the step, leaving
        # the tree no way back.
        from app.tools.firestore_tools import apply_engineer_correction

        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "never useful here",
            "set_hit_rate": {"agent": "teams_context_agent", "hit_rate": 0.0},
        }))
        teams = [s for s in firestore_store["sql-database"]["triage_steps"]
                 if s["agent"] == "teams_context_agent"][0]
        assert teams["hit_rate"] == HIT_RATE_FLOOR
        entry = firestore_store["sql-database"]["optimization_log"][-1]
        assert any("clamped from 0.0" in c for c in entry["changes"])

    def test_partial_reorder_promotes_named_and_preserves_the_rest(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "DBAs check blocking first",
            "reorder": ["sql_dba_agent"],
        }))
        steps = firestore_store["sql-database"]["triage_steps"]
        assert [s["agent"] for s in steps] == [
            "sql_dba_agent", "datadog_agent", "teams_context_agent"
        ]
        # `order` is renumbered so it cannot disagree with list position.
        assert [s["order"] for s in steps] == [1, 2, 3]

    def test_unlock_returns_a_step_to_the_automatic_path(self, firestore_store):
        from app.tools.firestore_tools import (
            apply_engineer_correction,
            record_incident_contributions,
        )

        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "demote",
            "set_hit_rate": {"agent": "teams_context_agent", "hit_rate": 0.2},
        }))
        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "let it learn again",
            "unlock": ["teams_context_agent"],
        }))
        teams = [s for s in firestore_store["sql-database"]["triage_steps"]
                 if s["agent"] == "teams_context_agent"][0]
        assert "human_locked" not in teams

        record_incident_contributions("sql-database", json.dumps(_incident(
            findings={"findings_teams": "Engineer posted revision `1000005-rc` at 14:02."},
            report="Rollout of `1000005-rc` matches the window.",
            incident_id="inc-after-unlock",
        )))
        teams = [s for s in firestore_store["sql-database"]["triage_steps"]
                 if s["agent"] == "teams_context_agent"][0]
        assert teams["hit_rate"] != 0.2

    def test_skip_rules_are_stored_as_operands_and_attributed(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "dev has no Datadog agent",
            "add_skip_rule": {
                "agent": "datadog_agent", "field": "environment",
                "op": "==", "value": "development",
            },
        }))
        rules = firestore_store["sql-database"]["skip_rules"]
        assert len(rules) == 1
        assert rules[0]["field"] == "environment"
        assert rules[0]["added_by"] == "sre"
        assert "condition" not in rules[0]     # never an expression string
        assert evaluate_skip_rule(rules[0], {"environment": "development"}) is True

    def test_skip_rule_without_field_or_op_is_rejected(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "typo",
            "add_skip_rule": {"condition": "environment == 'development'"},
        }))
        assert firestore_store["sql-database"]["skip_rules"] == []

    def test_remove_skip_rule(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        rule = {"agent": "datadog_agent", "field": "environment",
                "op": "==", "value": "development"}
        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "add", "add_skip_rule": rule,
        }))
        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "dev got Datadog", "remove_skip_rule": rule,
        }))
        assert firestore_store["sql-database"]["skip_rules"] == []

    def test_pin_and_unpin(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "hand-tuned, stop learning", "pinned": True,
        }))
        assert firestore_store["sql-database"]["optimization_pinned"] is True
        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "resume", "pinned": False,
        }))
        assert firestore_store["sql-database"]["optimization_pinned"] is False

    def test_every_correction_bumps_version_and_logs_who_and_why(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        apply_engineer_correction("sql-database", json.dumps({
            "actor": "first.last", "reason": "see postmortem 2026-07-30",
            "set_hit_rate": {"agent": "teams_context_agent", "hit_rate": 0.2},
        }))
        tree = firestore_store["sql-database"]
        assert tree["version"] == 2
        assert tree["last_optimized"] is not None
        entry = tree["optimization_log"][-1]
        assert entry["actor"] == "first.last"
        assert entry["note"] == "see postmortem 2026-07-30"
        assert entry["version"] == 2
        assert entry["changes"]

    def test_no_applicable_edit_is_a_no_op_not_a_version_bump(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        result = json.loads(apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "nothing to do here",
        })))
        assert result["status"] == "no_op"
        assert firestore_store["sql-database"]["version"] == 1

    def test_missing_tree_is_an_error(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        result = json.loads(apply_engineer_correction("nope", json.dumps({
            "actor": "sre", "reason": "x", "pinned": True,
        })))
        assert "error" in result


class TestUnknownFieldsSurvive:
    """A writer must never erase a field it does not know about.

    Live trees carry fields this code never names -- `monitoring-meta` has
    `optimization_note` (the prose record of a hand edit), `content-archive` has
    four more. A refactor to `tree_ref.set(tree)` would look equivalent, pass
    every other test here, and delete them.

    These run against the fake, so alone they only prove the writers call
    `update()` rather than `set()`. That `update()` itself preserves unnamed
    fields is a property of google-cloud-firestore 2.28.0 (uv.lock:1399-1400):
    `pbs_for_update` emits a `Write` whose `update_mask` names only the supplied
    paths. It also defaults to `ExistsOption(exists=True)`, which is why every
    writer reads the snapshot first instead of letting the update raise.
    """

    def test_automatic_pass_preserves_unknown_fields(self, firestore_store):
        from app.tools.firestore_tools import record_incident_contributions

        firestore_store["sql-database"].update({
            "optimization_note": "v2 2026-07-30 (human): teams demoted, see postmortem",
            "system_model": {"nodes": ["A", "B"]},
        })
        record_incident_contributions("sql-database", json.dumps(_incident(
            findings={"findings_datadog": "Monitor `10000001` shows a blocking chain."},
            report="Mechanism: blocking chain, per monitor `10000001`.",
            incident_id="inc-preserve-auto",
        )))
        tree = firestore_store["sql-database"]
        assert tree["optimization_note"].startswith("v2 2026-07-30")
        assert tree["system_model"] == {"nodes": ["A", "B"]}
        assert tree["version"] == 2  # and it really did write

    def test_human_correction_preserves_unknown_fields(self, firestore_store):
        from app.tools.firestore_tools import apply_engineer_correction

        firestore_store["sql-database"]["optimization_note"] = "hand-tuned 2026-07-30"
        apply_engineer_correction("sql-database", json.dumps({
            "actor": "sre", "reason": "demote teams",
            "set_hit_rate": {"agent": "teams_context_agent", "hit_rate": 0.2},
        }))
        assert firestore_store["sql-database"]["optimization_note"] == "hand-tuned 2026-07-30"

    def test_backfill_preserves_unknown_fields(self, firestore_store):
        from app.tools.firestore_tools import record_backfilled_hit_rates

        firestore_store["sql-database"]["optimization_note"] = "hand-tuned 2026-07-30"
        record_backfilled_hit_rates(
            "sql-database", json.dumps({"datadog_agent": [40, 66]}), 66, "sre"
        )
        assert firestore_store["sql-database"]["optimization_note"] == "hand-tuned 2026-07-30"

    def test_no_writer_ever_removes_a_triage_step(self, firestore_store):
        """ADO-200023's router intersects its tier-1 list with the tree's steps,
        so a dropped step silently drops that agent from every future run of the
        category, with no deploy. Nothing prunes today; this is the tripwire."""
        from app.tools.firestore_tools import record_incident_contributions

        before = [s["agent"] for s in firestore_store["sql-database"]["triage_steps"]]
        for i in range(3):
            record_incident_contributions("sql-database", json.dumps(_incident(
                findings={"findings_datadog": f"Monitor `1754004{i}` fired."},
                report=f"Per monitor `1754004{i}`.",
                incident_id=f"inc-noprune-{i}",
            )))
        after = [s["agent"] for s in firestore_store["sql-database"]["triage_steps"]]
        assert after == before

    def test_correction_paths_preserve_the_full_step_set(self, firestore_store):
        """The same guarantee for the human path, which is where it can break.

        `apply_engineer_correction` sorts and renumbers, and a reorder is where a
        bad sort key or a dedupe loses an entry. Partial loss is the dangerous
        shape: ADO-200023 fails open only on an EMPTY intersection, so dropping
        three of ten steps looks like deliberate tuning and is obeyed silently.
        """
        import collections

        from app.tools.firestore_tools import apply_engineer_correction

        # Must list one agent repeatedly: with distinct agents a
        # dedupe-by-agent-name prune removes nothing and the test cannot fail.
        # Compared as a multiset for the same reason.
        firestore_store["sql-database"]["triage_steps"] = [
            {"order": 1, "agent": "deploy_correlation_agent", "tier": 1, "hit_rate": 0.9},
            {"order": 2, "agent": "deploy_correlation_agent", "tier": 1, "hit_rate": 0.8},
            {"order": 3, "agent": "deploy_correlation_agent", "tier": 1, "hit_rate": 0.7},
            {"order": 4, "agent": "datadog_agent", "tier": 1, "hit_rate": 0.6},
            {"order": 5, "agent": "teams_context_agent", "tier": 1, "hit_rate": 0.5},
            {"order": 6, "agent": "sql_dba_agent", "tier": 2, "hit_rate": 0.5},
        ]
        before = collections.Counter(
            s["agent"] for s in firestore_store["sql-database"]["triage_steps"]
        )
        assert before["deploy_correlation_agent"] == 3  # the fixture must stay lossy
        for correction in (
            {"reorder": ["sql_dba_agent"]},                                  # partial
            {"reorder": ["teams_context_agent", "datadog_agent", "sql_dba_agent"]},  # full
            {"reorder": ["not_a_real_agent"]},                               # unknown name
            {"set_hit_rate": {"agent": "datadog_agent", "hit_rate": 0.9}},
            {"unlock": ["datadog_agent"]},
            {"add_skip_rule": {"agent": "datadog_agent", "field": "environment",
                               "op": "==", "value": "development"}},
            {"pinned": True},
        ):
            apply_engineer_correction("sql-database", json.dumps(
                {"actor": "sre", "reason": "no-prune audit", **correction}
            ))
            steps = firestore_store["sql-database"]["triage_steps"]
            assert collections.Counter(s["agent"] for s in steps) == before, correction
            # Dense 1..N, or a consumer reading `order` disagrees with the list.
            assert [s["order"] for s in steps] == list(range(1, len(steps) + 1)), correction


class TestBackfillWriter:
    def test_backfill_writes_and_marks_itself(self, firestore_store):
        from app.tools.firestore_tools import record_backfilled_hit_rates

        result = json.loads(record_backfilled_hit_rates(
            "sql-database", json.dumps({"datadog_agent": [40, 66]}), 66, "first.last"
        ))
        assert result["status"] == "success"
        tree = firestore_store["sql-database"]
        assert tree["incident_count"] == 66
        assert tree["optimization_log"][-1]["backfill"] is True
        assert tree["optimization_log"][-1]["incidents_analyzed"] == 66
        datadog = tree["triage_steps"][0]
        assert datadog["hit_rate"] == posterior_hit_rate(40, 66)

    def test_backfill_refuses_without_an_actor(self, firestore_store):
        from app.tools.firestore_tools import record_backfilled_hit_rates

        result = json.loads(record_backfilled_hit_rates(
            "sql-database", json.dumps({"datadog_agent": [40, 66]}), 66, ""
        ))
        assert "error" in result
        assert firestore_store["sql-database"]["version"] == 1

    def test_backfill_respects_a_pin(self, firestore_store):
        from app.tools.firestore_tools import record_backfilled_hit_rates

        firestore_store["sql-database"]["optimization_pinned"] = True
        result = json.loads(record_backfilled_hit_rates(
            "sql-database", json.dumps({"datadog_agent": [40, 66]}), 66, "sre"
        ))
        assert result["status"] == "pinned"
        assert firestore_store["sql-database"]["version"] == 1

    def test_backfill_respects_a_human_lock(self, firestore_store):
        from app.tools.firestore_tools import record_backfilled_hit_rates

        firestore_store["sql-database"]["triage_steps"][1].update({
            "hit_rate": 0.2, "human_locked": True, "human_locked_by": "first.last",
        })
        record_backfilled_hit_rates(
            "sql-database",
            json.dumps({"datadog_agent": [40, 66], "teams_context_agent": [60, 66]}),
            66, "sre",
        )
        teams = firestore_store["sql-database"]["triage_steps"][1]
        assert teams["hit_rate"] == 0.2

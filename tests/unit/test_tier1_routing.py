"""Tier 1 decision-tree routing (ADO-200023).

Two silent failure shapes drive most of these tests:

  1. A skipped agent leaving `{findings_*}` unset. synthesis.py and report.py
     read those keys directly and an unset key renders as "", which reads as
     "checked, found nothing". Hence the sentinel (AC 5).
  2. Routing that fails CLOSED. An unreachable Firestore, unknown category, or
     malformed document must never stop an investigation (AC 7).
"""

import json

import pytest

from app.categories import CATEGORIES
from app.tier1_routing import (
    ALL_TIER1_AGENTS,
    DEFAULT_TIER1_SELECTION,
    TIER1_AGENT_FINDINGS_KEYS,
    TREE_AGENT_TO_ADK_AGENT,
    SkipRuleError,
    build_tier1_plan,
    evaluate_skip_condition,
    routing_note,
    skip_sentinel,
)


def _tree(category, steps, *, version=1, skip_rules=None):
    """A get_triage_guidance-shaped JSON string."""
    return json.dumps({
        "category": category,
        "version": version,
        "incident_count": 0,
        "last_optimized": None,
        "triage_steps": steps,
        "skip_rules": skip_rules or [],
    })


def _step(order, agent, action, *, tier=1, hit_rate=0.5, skip_rules=None):
    step = {"order": order, "agent": agent, "action": action, "tier": tier, "hit_rate": hit_rate}
    if skip_rules is not None:
        step["skip_rules"] = skip_rules
    return step


# The live content-archive tree's tier-1 steps, verified against
# aiops-dev-project on 2026-08-20. The only curated tree in the collection.
ARCHIVE_SITES_STEPS = [
    _step(0, "historical_context_agent", "ORIENT (read first)", hit_rate=1.0),
    _step(1, "deploy_correlation_agent", "NODE A - true no-run? get_builds(def=2451)", hit_rate=0.9),
    _step(2, "deploy_correlation_agent", "NODE B - did scheduler fire?", hit_rate=0.8),
    _step(6, "datadog_agent", "NODE E - verify current prod deploy branch", hit_rate=0.6),
    _step(7, "known_issues_agent", "Search ADO for content-archive bugs", hit_rate=0.4),
    _step(8, "codebase_agent", "TIER 2: inspect pipeline YAML", tier=2, hit_rate=0.6),
]


class TestSelectionTablesAreCoherent:
    """The routing tables must line up with the rest of the system."""

    def test_every_category_has_a_selection(self):
        assert set(DEFAULT_TIER1_SELECTION) == set(CATEGORIES)

    def test_no_category_selects_zero_agents(self):
        """AC 8. An empty selection is a bug, not a valid tree."""
        for category, agents in DEFAULT_TIER1_SELECTION.items():
            assert agents, category

    def test_preferences_name_only_real_tier1_agents(self):
        for category, agents in DEFAULT_TIER1_SELECTION.items():
            assert set(agents) <= set(ALL_TIER1_AGENTS), category

    def test_no_selection_lists_a_duplicate(self):
        for category, agents in DEFAULT_TIER1_SELECTION.items():
            assert len(agents) == len(set(agents)), category

    def test_every_tier1_agent_has_a_findings_key(self):
        assert set(TIER1_AGENT_FINDINGS_KEYS) == set(ALL_TIER1_AGENTS)

    def test_findings_keys_are_the_ones_synthesis_reads(self):
        """These exact strings appear in synthesis.py and report.py."""
        assert set(TIER1_AGENT_FINDINGS_KEYS.values()) == {
            "findings_cloud_run", "findings_compute_engine", "findings_gcp_logging",
            "findings_gcp_metrics", "findings_datadog", "findings_sentry",
            "findings_deploy", "findings_teams", "findings_known_issues",
        }

    def test_tree_name_map_covers_every_tier1_agent(self):
        """The trees speak `*_agent`; the ADK agents kept their pre-Tier-B names."""
        assert set(TREE_AGENT_TO_ADK_AGENT.values()) == set(ALL_TIER1_AGENTS)

    def test_membership_matches_the_actual_parallel_agent(self):
        from app.sub_agents.tier1.parallel import tier1_parallel

        assert {a.name for a in tier1_parallel.sub_agents} == set(ALL_TIER1_AGENTS)


class TestPerCategorySelection:
    """AC 2: Tier 1 is built per-run from the category's agent list."""

    def test_a_narrowed_category_does_not_run_all_nine(self):
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        assert not plan["fail_open"]
        assert len(plan["selected"]) < len(ALL_TIER1_AGENTS)

    def test_archive_sites_selects_exactly_its_tier1_steps(self):
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        assert plan["selected"] == ["deploy_correlation", "datadog_observability", "known_issues"]

    def test_tier2_steps_do_not_select_a_tier1_agent(self):
        """codebase_agent is Tier 2 and is not a Tier 1 agent at all."""
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        assert "codebase_agent" not in plan["selected"]

    def test_selection_follows_tree_order_not_table_order(self):
        """The tree is the tuning knob; its ordering is a deliberate signal."""
        steps = [
            _step(1, "known_issues_agent", "first"),
            _step(2, "gcp_logging_agent", "second"),
            _step(3, "sentry_agent", "third"),
        ]
        plan = build_tier1_plan("application-errors", _tree("application-errors", steps), {})
        assert plan["selected"][:3] == ["known_issues", "gcp_logging", "sentry_error"]

    def test_out_of_order_step_numbers_are_sorted(self):
        steps = [
            _step(9, "sentry_agent", "last"),
            _step(1, "gcp_logging_agent", "first"),
        ]
        plan = build_tier1_plan("application-errors", _tree("application-errors", steps), {})
        assert plan["selected"].index("gcp_logging") < plan["selected"].index("sentry_error")

    def test_deleting_a_step_cannot_be_the_only_narrowing(self):
        """A Firestore name that resolves to no ADK agent is a typo, not an
        instruction to add one."""
        steps = [
            _step(1, "gcp_logging_agent", "run me"),
            _step(2, "teams_context_agent", "not in the sql-database selection"),
        ]
        plan = build_tier1_plan("sql-database", _tree("sql-database", steps), {})
        assert "teams_context" not in plan["selected"]

    def test_an_agent_the_tree_omits_is_still_run(self):
        """A stale tree must not read as a deliberate exclusion."""
        steps = [_step(1, "gcp_logging_agent", "only step")]
        plan = build_tier1_plan("sql-database", _tree("sql-database", steps), {})
        for agent in DEFAULT_TIER1_SELECTION["sql-database"]:
            assert agent in plan["selected"], agent

    def test_skipped_plus_selected_covers_all_nine(self):
        """No agent may fall through the plan unaccounted for."""
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        accounted = set(plan["selected"]) | {s["agent"] for s in plan["skipped"]}
        assert accounted == set(ALL_TIER1_AGENTS)

    def test_every_skip_carries_a_reason(self):
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        assert plan["skipped"]
        for entry in plan["skipped"]:
            assert entry["reason"].strip()

    @pytest.mark.parametrize("category", sorted(DEFAULT_TIER1_SELECTION))
    def test_no_category_ever_selects_zero_agents_against_its_seed_tree(self, category):
        """AC 8, against the real seed data rather than a fixture."""
        import pathlib

        seeds = json.loads(
            (
                pathlib.Path(__file__).resolve().parents[2]
                / "app" / "data" / "decision_tree_seeds.json"
            ).read_text()
        )
        seed = next((s for s in seeds if s["category"] == category), None)
        guidance = None if seed is None else json.dumps(seed)
        plan = build_tier1_plan(category, guidance, {})
        assert plan["selected"], category


class TestActionTextReachesTheAgent:
    """AC 3: each selected agent receives its step's `action` as focus."""

    def test_focus_carries_the_trees_action_text(self):
        steps = [_step(1, "gcp_logging_agent", "Search for AppSqlException and deadlock events")]
        plan = build_tier1_plan("sql-database", _tree("sql-database", steps), {})
        assert plan["focus"]["gcp_logging"] == "Search for AppSqlException and deadlock events"

    def test_focus_is_only_populated_for_selected_agents(self):
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        assert set(plan["focus"]) <= set(plan["selected"])

    def test_first_step_wins_when_an_agent_appears_twice(self):
        """content-archive lists deploy_correlation on NODE A, B, C, D and F."""
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        assert plan["focus"]["deploy_correlation"].startswith("NODE A")

    def test_an_empty_action_produces_no_focus_key(self):
        steps = [_step(1, "gcp_logging_agent", "   ")]
        plan = build_tier1_plan("sql-database", _tree("sql-database", steps), {})
        assert "gcp_logging" not in plan["focus"]

    def test_the_focus_prompt_section_exists_on_every_tier1_agent(self):
        from app.sub_agents.tier1.parallel import tier1_parallel
        from app.tier1_routing import TIER1_FOCUS_KEY_PREFIX

        for agent in tier1_parallel.sub_agents:
            expected = "{" + TIER1_FOCUS_KEY_PREFIX + agent.name + "?}"
            assert expected in agent.instruction, agent.name

    def test_focus_reference_is_optional_so_a_missing_key_cannot_crash_a_run(self):
        """ADK raises KeyError on `{x}` when x is absent; `{x?}` renders empty."""
        from app.sub_agents.tier1.parallel import tier1_parallel
        from app.tier1_routing import TIER1_FOCUS_KEY_PREFIX

        for agent in tier1_parallel.sub_agents:
            required = "{" + TIER1_FOCUS_KEY_PREFIX + agent.name + "}"
            assert required not in agent.instruction, agent.name


class TestSkipRuleEvaluator:
    """AC 4: skip_rules evaluate against classification fields, with no eval()."""

    def test_equality_fires(self):
        assert evaluate_skip_condition("environment == 'dev'", {"environment": "dev"})

    def test_equality_does_not_fire(self):
        assert not evaluate_skip_condition("environment == 'dev'", {"environment": "prod"})

    def test_inequality(self):
        assert evaluate_skip_condition("environment != 'prod'", {"environment": "dev"})
        assert not evaluate_skip_condition("environment != 'prod'", {"environment": "prod"})

    def test_membership(self):
        cond = "severity in ['sev3', 'sev4']"
        assert evaluate_skip_condition(cond, {"severity": "sev3"})
        assert not evaluate_skip_condition(cond, {"severity": "sev1"})

    def test_negated_membership(self):
        cond = "environment not in ['prod', 'staging']"
        assert evaluate_skip_condition(cond, {"environment": "dev"})
        assert not evaluate_skip_condition(cond, {"environment": "prod"})

    def test_comparison_is_case_insensitive(self):
        """The classifier emits 'prod'/'sev2'; a hand-written rule may say 'Prod'."""
        assert evaluate_skip_condition("environment == 'PROD'", {"environment": "prod"})
        assert evaluate_skip_condition("severity in ['SEV3']", {"severity": "sev3"})

    def test_a_missing_field_does_not_fire_an_equality_rule(self):
        assert not evaluate_skip_condition("environment == 'dev'", {})

    @pytest.mark.parametrize("field", ["severity", "environment", "category", "service_name"])
    def test_every_allowed_field_is_testable(self, field):
        assert evaluate_skip_condition(f"{field} == 'x'", {field: "x"})

    def test_an_unknown_field_is_rejected(self):
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition("hostname == 'x'", {"hostname": "x"})

    @pytest.mark.parametrize("payload", [
        "__import__('os').system('echo pwned')",
        "severity == __import__('os').getcwd()",
        "().__class__.__bases__[0].__subclasses__()",
        "severity == 'sev1' or True",
        "exec('x=1')",
        "open('/etc/passwd').read()",
    ])
    def test_code_in_a_stored_condition_never_executes(self, payload):
        """These strings come from editable Firestore rows, so eval() here would
        be code execution via a database edit. Every payload must raise."""
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition(payload, {"severity": "sev1"})

    def test_a_non_string_literal_is_rejected(self):
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition("severity == 3", {"severity": "3"})

    def test_a_list_of_non_strings_is_rejected(self):
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition("severity in [1, 2]", {"severity": "1"})

    def test_a_list_literal_is_rejected_for_equality(self):
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition("severity == ['sev1']", {"severity": "sev1"})

    def test_a_string_literal_is_rejected_for_membership(self):
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition("severity in 'sev1'", {"severity": "sev1"})

    def test_gibberish_is_rejected(self):
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition("this is not a condition", {})

    def test_an_empty_condition_is_rejected(self):
        with pytest.raises(SkipRuleError):
            evaluate_skip_condition("", {})


class TestSkipRulesFiring:
    """A firing rule removes the agent; a broken rule never does."""

    def test_a_step_level_rule_removes_its_agent(self):
        steps = [
            _step(1, "gcp_logging_agent", "always"),
            _step(
                2, "datadog_agent", "dev has no DD",
                skip_rules=[{"condition": "environment == 'dev'", "reason": "Dev DBs have no Datadog"}],
            ),
        ]
        plan = build_tier1_plan(
            "sql-database", _tree("sql-database", steps), {"environment": "dev"}
        )
        assert "datadog_observability" not in plan["selected"]
        assert "gcp_logging" in plan["selected"]

    def test_the_rules_reason_reaches_the_skip_entry(self):
        steps = [_step(
            1, "datadog_agent", "x",
            skip_rules=[{"condition": "environment == 'dev'", "reason": "Dev DBs have no Datadog"}],
        )]
        plan = build_tier1_plan(
            "sql-database", _tree("sql-database", steps), {"environment": "dev"}
        )
        reason = next(
            s["reason"] for s in plan["skipped"] if s["agent"] == "datadog_observability"
        )
        assert "Dev DBs have no Datadog" in reason
        assert "environment == 'dev'" in reason

    def test_a_rule_that_does_not_match_leaves_the_agent_in(self):
        steps = [_step(
            1, "datadog_agent", "x",
            skip_rules=[{"condition": "environment == 'dev'", "reason": "no DD in dev"}],
        )]
        plan = build_tier1_plan(
            "sql-database", _tree("sql-database", steps), {"environment": "prod"}
        )
        assert "datadog_observability" in plan["selected"]

    def test_a_tree_level_rule_applies_to_every_step(self):
        """The live documents carry skip_rules at tree level, not step level."""
        steps = [_step(1, "gcp_logging_agent", "x"), _step(2, "datadog_agent", "y")]
        plan = build_tier1_plan(
            "sql-database",
            _tree("sql-database", steps,
                  skip_rules=[{"condition": "severity == 'sev4'", "reason": "informational only"}]),
            {"severity": "sev4"},
        )
        # Every tree step is rule-skipped, so nothing was selected from the tree
        # and AC 8 fails the run open rather than investigating nothing.
        assert plan["fail_open"]
        assert plan["selected"] == list(ALL_TIER1_AGENTS)

    def test_an_unparseable_rule_is_ignored_not_treated_as_firing(self):
        """A rule nobody can read is a config defect, not an instruction."""
        steps = [_step(
            1, "datadog_agent", "x",
            skip_rules=[{"condition": "environment ~~ dev", "reason": "typo"}],
        )]
        plan = build_tier1_plan(
            "sql-database", _tree("sql-database", steps), {"environment": "dev"}
        )
        assert "datadog_observability" in plan["selected"]
        assert any("skip rule ignored" in n for n in plan["notes"])

    def test_a_rule_naming_an_unknown_field_is_ignored(self):
        steps = [_step(
            1, "datadog_agent", "x",
            skip_rules=[{"condition": "region == 'us-central1'", "reason": "wrong region"}],
        )]
        plan = build_tier1_plan("sql-database", _tree("sql-database", steps), {})
        assert "datadog_observability" in plan["selected"]


class TestFailOpen:
    """AC 7: routing never blocks an investigation.

    All seven causes produce the same shape (9 agents, nothing skipped,
    fail_open True), so asserting only on the shape is inert -- collapsing every
    `_parse_guidance` diagnosis to one string left the whole suite green. Each
    case asserts the shape AND its own cause.
    """

    def _assert_ran_everything(self, plan, because):
        """`because` is required: it is the only assertion here that tells one
        fail-open cause from another."""
        assert plan["fail_open"] is True
        assert plan["selected"] == list(ALL_TIER1_AGENTS)
        assert plan["skipped"] == []
        assert plan["notes"], "fail-open must say why"
        joined = " | ".join(plan["notes"])
        assert because in joined, f"expected reason {because!r} in notes: {joined!r}"

    def test_tree_never_fetched(self):
        self._assert_ran_everything(
            build_tier1_plan("sql-database", None, {}),
            because="decision tree was not fetched",
        )

    def test_firestore_error_response(self):
        """A 503 must stay distinguishable from 'this category has no tree': one is
        an outage to escalate, the other is normal for an unseeded category."""
        guidance = json.dumps({"error": "get_triage_guidance failed: 503 unavailable"})
        self._assert_ran_everything(
            build_tier1_plan("sql-database", guidance, {}),
            because="503 unavailable",
        )

    def test_no_document_for_the_category(self):
        """get_triage_guidance's real 'no doc' shape: a message, no triage_steps."""
        guidance = json.dumps({
            "category": "sql-database",
            "guidance": "No learned triage guidance for category 'sql-database' yet.",
        })
        self._assert_ran_everything(
            build_tier1_plan("sql-database", guidance, {}),
            because="No learned triage guidance",
        )

    def test_malformed_json(self):
        self._assert_ran_everything(
            build_tier1_plan("sql-database", "{not json", {}),
            because="was not JSON",
        )

    def test_json_that_is_not_an_object(self):
        self._assert_ran_everything(
            build_tier1_plan("sql-database", "[1, 2, 3]", {}),
            because="not a JSON object",
        )

    def test_triage_steps_is_not_a_list(self):
        guidance = json.dumps({"version": 1, "triage_steps": "oops"})
        self._assert_ran_everything(
            build_tier1_plan("sql-database", guidance, {}),
            because="no usable triage_steps",
        )

    def test_triage_steps_is_empty(self):
        self._assert_ran_everything(
            build_tier1_plan("sql-database", _tree("sql-database", []), {}),
            because="no usable triage_steps",
        )

    def test_a_category_with_no_selection_table_entry(self):
        self._assert_ran_everything(
            build_tier1_plan("brand-new-category", _tree("x", [_step(1, "gcp_logging_agent", "a")]), {}),
            because="brand-new-category",
        )

    def test_an_empty_category_string(self):
        self._assert_ran_everything(
            build_tier1_plan("", None, {}),
            because="decision tree was not fetched",
        )

    def test_every_fail_open_cause_states_a_DISTINCT_reason(self):
        """Per-case assertions run independently, so two causes could converge on
        one string containing both substrings. Compare as a set."""
        causes = {
            "not fetched": build_tier1_plan("sql-database", None, {}),
            "firestore error": build_tier1_plan(
                "sql-database", json.dumps({"error": "503 unavailable"}), {}),
            "no document": build_tier1_plan("sql-database", json.dumps({
                "category": "sql-database",
                "guidance": "No learned triage guidance yet."}), {}),
            "bad json": build_tier1_plan("sql-database", "{not json", {}),
            "not an object": build_tier1_plan("sql-database", "[1, 2, 3]", {}),
            "no steps": build_tier1_plan("sql-database", _tree("sql-database", []), {}),
            "unknown category": build_tier1_plan("brand-new-category", None, {}),
        }
        reasons = {name: " | ".join(p["notes"]) for name, p in causes.items()}
        assert len(set(reasons.values())) == len(reasons), (
            "two fail-open causes report the SAME reason, so an operator cannot tell "
            f"them apart: {reasons}"
        )

    def test_the_source_field_separates_a_tree_failure_from_a_missing_category(self):
        """A dashboard groups on tree_source, so it must discriminate too."""
        unreachable = build_tier1_plan("sql-database", None, {})
        unknown_cat = build_tier1_plan("brand-new-category", None, {})

        assert unreachable["tree_source"] == "tree-unavailable"
        assert unknown_cat["tree_source"] == "unknown-category"
        assert unreachable["tree_source"] != unknown_cat["tree_source"]

    def test_a_tree_naming_only_unknown_agents_fails_open(self):
        """AC 8. Zero selected is a bug in the tree, not 'investigate nothing'."""
        steps = [_step(1, "nonexistent_agent", "a"), _step(2, "another_fake_agent", "b")]
        plan = build_tier1_plan("content-archive", _tree("content-archive", steps), {})
        # content-archive' in-repo list survives because the tree omits it entirely
        # (a stale tree is not an exclusion), so this is a normal narrowed plan.
        assert plan["selected"] == DEFAULT_TIER1_SELECTION["content-archive"]

    def test_a_tree_naming_only_tier2_agents_keeps_the_in_repo_list(self):
        steps = [_step(1, "sql_dba_agent", "a", tier=2)]
        plan = build_tier1_plan("sql-database", _tree("sql-database", steps), {})
        assert plan["selected"] == DEFAULT_TIER1_SELECTION["sql-database"]

    def test_steps_with_junk_shapes_do_not_raise(self):
        guidance = json.dumps({
            "version": 1,
            "triage_steps": [
                "a bare string",
                None,
                {"agent": None},
                {"agent": "gcp_logging_agent", "action": "ok", "order": "not-a-number"},
            ],
        })
        plan = build_tier1_plan("sql-database", guidance, {})
        assert plan["selected"]

    def test_a_step_with_a_junk_tier_is_treated_as_tier1(self):
        """The agent name is the stronger signal; defaulting the other way drops it."""
        steps = [{"order": 1, "agent": "gcp_logging_agent", "action": "a", "tier": "one"}]
        plan = build_tier1_plan("sql-database", _tree("sql-database", steps), {})
        assert plan["focus"].get("gcp_logging") == "a"


class TestSkipSentinel:
    """AC 5: a skipped agent's findings key says so, in words."""

    def _plan(self):
        return build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS, version=3), {}
        )

    def test_sentinel_says_not_run(self):
        assert skip_sentinel("sentry_error", self._plan()).startswith("NOT RUN")

    def test_sentinel_carries_the_tree_version(self):
        """AC 6: the report must be able to name the tree that decided it."""
        assert "v3" in skip_sentinel("sentry_error", self._plan())

    def test_sentinel_carries_the_reason(self):
        assert "content-archive" in skip_sentinel("sentry_error", self._plan())

    def test_sentinel_forbids_reading_a_skip_as_an_empty_result(self):
        """The whole hazard: "" reads to synthesis as 'checked, found nothing'."""
        text = skip_sentinel("sentry_error", self._plan())
        assert "not evidence of absence" in text
        assert "not a tool failure" in text

    def test_sentinel_forbids_counting_a_skip_as_a_source(self):
        text = skip_sentinel("sentry_error", self._plan())
        assert "cross-validation" in text

    def test_sentinel_survives_an_agent_missing_from_the_skip_list(self):
        assert skip_sentinel("gcp_logging", {"selected": [], "skipped": []})

    def test_a_fail_open_plan_names_the_fallback_not_a_version(self):
        plan = build_tier1_plan("sql-database", None, {})
        assert "tree-unavailable" in skip_sentinel("sentry_error", plan)


class TestRoutingNote:
    """AC 6: the report names ran, skipped, and the deciding tree version."""

    def test_note_names_the_version(self):
        note = routing_note(build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS, version=7), {}
        ))
        assert "decision tree v7" in note

    def test_note_lists_what_ran(self):
        note = routing_note(build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        ))
        assert "ran: deploy_correlation" in note

    def test_note_lists_what_was_skipped(self):
        note = routing_note(build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        ))
        assert "skipped: " in note
        assert "sentry_error" in note.split("skipped: ", 1)[1]

    def test_a_fail_open_run_says_fail_open(self):
        note = routing_note(build_tier1_plan("sql-database", None, {}))
        assert "FAIL-OPEN" in note
        assert "skipped: none" in note

    def test_the_note_is_a_single_line(self):
        """It goes to the log, so it must not wrap."""
        note = routing_note(build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        ))
        assert "\n" not in note


class TestPlanIsSerialisable:
    """It goes into ADK session state and through persistence."""

    def test_plan_round_trips_through_json(self):
        plan = build_tier1_plan(
            "content-archive", _tree("content-archive", ARCHIVE_SITES_STEPS), {}
        )
        assert json.loads(json.dumps(plan)) == plan


class TestDecisions:
    """Pins two review decisions so a revert fails here."""

    def test_no_category_selects_teams_context(self):
        # Its prompt makes no tool calls, so it cannot return evidence.
        # "unknown" is the fail-open catch-all and carries all 9 by design.
        for category, agents in DEFAULT_TIER1_SELECTION.items():
            if category == "unknown":
                continue
            assert "teams_context" not in agents, category

    def test_the_report_prompt_does_not_render_the_routing_note(self):
        # Which agents ran is telemetry: tree_router logs it, the report omits it.
        from app.sub_agents.report import REPORT_INSTRUCTION

        assert "Coverage:" not in REPORT_INSTRUCTION
        assert "tier1_routing_note" not in REPORT_INSTRUCTION

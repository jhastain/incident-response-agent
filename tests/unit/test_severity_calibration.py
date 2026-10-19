"""Regression tests for severity misclassification — ADO-200012.

The story reported "severity is wrong". Measurement over 124 stored incidents in
Firestore (`aiops-test-project`, collection `incidents`) showed it is TWO problems
with different fixes, and these tests pin both.

**Problem 1 — nondeterminism.** 22 groups of byte-identical alert text were run 2+
times (95 docs). On input that did not change at all, the classifier disagreed with
itself: severity 7.4% (7/95), service 5.3% (5/95), category 4.2% (4/95). No
`generate_content_config` was passed to ANY agent, so every run sampled at the
provider default temperature (1.0 for Anthropic). Pinned at `classifier.py`.

**Problem 2 — one genuine miscalibration.** Live per-scenario distributions:

    cloud-infrastructure-startup-probe  sev2 x3, sev3 x1  (ground truth sev3)  <- MODE WRONG
    application-errors-apigateway-mobile      sev2 x4, sev3 x1  (ground truth sev2)  <- 80% correct
    sql-database-preferences-blocking    sev2 x4           (ground truth sev2)  <- stable

Only startup-probe is miscalibrated. apigateway-mobile is right 80% of the time and the
n=3 benchmark simply drew its tail — tuning against it would be fitting to noise.

The startup-probe cause was a self-cancelling modifier: "non-prod is typically sev3
or sev4", immediately re-opened by a `However` clause granting sev2 to anything that
"blocks CI/CD pipelines". A stalled deploy literally does, so the rule argued FOR
sev2 when ground truth is sev3 (7 of 8 services healthy, one stuck in a test
project). Replaced with a general blast-radius heuristic — no benchmark tokens.

**Problem 3 — vocabulary mismatch.** `classifier.py` can only emit sev1-sev4, but
`report.py` and `synthesis.py` both mapped `critical/high/medium/low`. The report
agent was told to map words that never arrive (so emoji choice was unconstrained)
and synthesis's severity-calibration step compared against a vocabulary the
classifier does not speak. Both realigned to sev1-sev4.

These are the first tests in the suite that assert anything about
CLASSIFIER_INSTRUCTION — before this the classifier prompt was entirely unguarded.
"""

import json
import pathlib
import re

import pytest

from app.sub_agents.classifier import (
    CLASSIFIER_INSTRUCTION,
    AlertClassification,
    alert_classifier,
)
from app.sub_agents.report import REPORT_INSTRUCTION
from app.sub_agents.synthesis import SYNTHESIS_INSTRUCTION

SEV_LEVELS = ("sev1", "sev2", "sev3", "sev4")

# The word-scale the classifier does NOT speak. It survives ONLY as an explicit
# "maps from" hint in the classifier (alerts do carry these words) and as the
# named-and-refuted anti-pattern in the downstream prompts.
WORD_SCALE = ("critical", "high", "medium", "low")


def _flat(text: str) -> str:
    """Collapse prompt line-wrapping so a prose assertion is not a formatting test."""
    return " ".join(text.split())


def _severity_section(instruction: str) -> str:
    """The severity block of the classifier prompt, up to the next top-level bullet."""
    flat = _flat(instruction)
    start = flat.index("- **Severity**")
    end = flat.index("- **Service Name**", start)
    return flat[start:end]


class TestSamplingIsPinnedOnTheClassifier:
    """Problem 1. Measured 7.4% severity flip rate on byte-identical input."""

    def test_classifier_pins_temperature_to_zero(self):
        config = alert_classifier.generate_content_config

        assert config is not None, (
            "no generate_content_config on the classifier — it samples at the provider "
            "default (1.0 for Anthropic), which is the measured 7.4% flip rate"
        )
        assert config.temperature == 0.0

    def test_temperature_is_a_forwarded_litellm_parameter(self):
        """The premise of the fix: ADK actually forwards it.

        `lite_llm._get_completion_inputs` builds generation_params from an explicit
        allow-list. If `temperature` ever leaves that list the pin becomes a silent
        no-op — the same mechanism that stops `thinking_config` reaching the model
        (see verification.py). Assert on the real installed source.
        """
        import inspect

        from google.adk.models import lite_llm

        src = inspect.getsource(lite_llm._get_completion_inputs)

        assert '"temperature"' in src, (
            "ADK no longer forwards temperature to LiteLLM — the classifier pin is a no-op"
        )

    def test_pin_survives_into_the_llm_request(self):
        """ADK copies the agent config onto the request in `_build_basic_request`."""
        import inspect

        from google.adk.flows.llm_flows import basic

        src = inspect.getsource(basic._build_basic_request)

        assert "agent.generate_content_config" in src

    def test_scope_is_the_classifier_only(self):
        """A system-wide determinism change is a bigger story than this one.

        The investigation agents plausibly benefit from exploration in tool-call
        sequencing and the report agent from prose variety. If someone pins those,
        it must be a deliberate decision with its own before/after measurement —
        not a side effect of this fix.
        """
        from app.sub_agents.report import report_agent
        from app.sub_agents.synthesis import synthesis_agent
        from app.sub_agents.verification import verification_agent

        for agent in (report_agent, synthesis_agent, verification_agent):
            assert agent.generate_content_config is None, (
                f"{agent.name} was pinned too — that is a system-wide behaviour change "
                "and needs its own work item and its own measurement (ADO-200012)"
            )


class TestAllFourSeverityLevelsAreDefined:
    """The contract test the suite never had: every level the schema permits is taught."""

    @pytest.mark.parametrize("level", SEV_LEVELS)
    def test_level_is_defined_in_the_instruction(self, level):
        assert f'"{level}"' in CLASSIFIER_INSTRUCTION or f"`{level}`" in CLASSIFIER_INSTRUCTION

    @pytest.mark.parametrize("level", SEV_LEVELS)
    def test_level_is_defined_in_the_output_schema(self, level):
        description = AlertClassification.model_fields["severity"].description

        assert level in description

    @pytest.mark.parametrize("level", SEV_LEVELS)
    def test_level_has_a_worked_example(self, level):
        """Before this story every example was sev2 or sev3 — no sev1, no sev4.

        The live corpus is 73 sev3 / 48 sev2 / 2 sev4 / 1 sev1, i.e. the scale is
        effectively two-valued in practice. A level with no exemplar is a level the
        model has never been shown how to reach.
        """
        examples = CLASSIFIER_INSTRUCTION.split("## Examples", 1)[1]

        assert f'severity: "{level}"' in examples, f"no worked example produces {level}"

    def test_examples_cover_both_ends_with_a_stated_reason(self):
        """An example without a rationale teaches the answer, not the rule."""
        examples = _flat(CLASSIFIER_INSTRUCTION.split("## Examples", 1)[1])

        assert "Why sev1 and not sev2" in examples
        assert "Why sev3 and not sev2" in examples
        assert "Why sev4 and not sev3" in examples


class TestTheSelfCancellingModifierIsGone:
    """Problem 2. The `However` clause that argued for the wrong answer."""

    def test_blocking_cicd_is_no_longer_a_standalone_route_to_sev2(self):
        """The exact defect: "blocks CI/CD pipelines" as an unqualified sev2 grant.

        The startup-probe alert (a stalled deploy, 7 of 8 services healthy, test
        project) satisfies it literally, which is why sev2 was the 75% mode against
        a sev3 ground truth.
        """
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "blocks CI/CD pipelines, or causes widespread test failures" not in section
        assert "However, use sev2 when" not in section

    def test_blocked_work_alone_does_not_raise_severity(self):
        """The replacement must say so explicitly, not merely omit the old clause."""
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "does NOT by itself lift a non-prod alert to sev2" in section

    def test_minority_of_a_fleet_is_an_isolated_issue(self):
        """The general rule that produces sev3 for the startup-probe shape."""
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "One stuck instance among healthy peers" in section
        assert "isolated issue" in section
        assert "sev3" in section

    def test_non_prod_still_skews_low(self):
        """Corpus-wide the modifier holds: test skews sev3 (24/13/1), prod sev2 (31/21/1).

        The rule was ambiguous where two clauses collided, not broadly wrong — so the
        rewrite must keep the low-skew, not delete it.
        """
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "typically sev3" in section
        assert "never sev1" in section

    def test_sev2_in_non_prod_still_reachable_for_broad_impact(self):
        """apigateway-mobile is env=test, ground truth sev2, and already 80% correct.

        A blanket "non-prod caps at sev3" would fit the noise on startup-probe and
        REGRESS the scenario that is already right. sev2 must stay reachable in
        non-prod when the blast radius is genuinely broad.
        """
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "Use sev2 in non-production ONLY when" in section
        assert "MAJORITY" in section

    def test_the_three_general_factors_are_named(self):
        """User impact, blast radius, data loss — general, not scenario-shaped."""
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "User impact" in section
        assert "Blast radius" in section
        assert "Data loss or corruption" in section

    def test_severity_is_not_driven_by_alert_tone(self):
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "Never raise severity because the alert text sounds urgent" in section

    def test_the_surviving_modifiers_are_unchanged_in_effect(self):
        """Surgical: the two non-defective modifiers keep their meaning."""
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert "single service or single endpoint failing in production is sev2, not sev1" in section
        assert "no full outage is sev2, not sev1" in section
        assert 'Default to "sev2" if uncertain.' in section


class TestSeverityVocabularyIsAligned:
    """Problem 3. Downstream prompts spoke a scale the classifier cannot emit."""

    @pytest.mark.parametrize("level", SEV_LEVELS)
    def test_report_emoji_map_keys_are_what_the_classifier_emits(self, level):
        emoji_section = REPORT_INSTRUCTION.split("## Severity Emojis", 1)[1].split("##", 1)[0]

        assert f"`{level}`" in emoji_section, f"report emoji map has no {level} key"

    def test_report_emoji_map_no_longer_keys_on_the_word_scale(self):
        """Verbatim the old broken map: `critical` -> red, etc. No key ever matched."""
        emoji_section = REPORT_INSTRUCTION.split("## Severity Emojis", 1)[1].split("##", 1)[0]

        for word in WORD_SCALE:
            assert f"- `{word}` →" not in emoji_section, (
                f"report still maps `{word}`, which the classifier never emits"
            )

    def test_report_maps_each_level_to_a_distinct_emoji(self):
        emoji_section = REPORT_INSTRUCTION.split("## Severity Emojis", 1)[1].split("##", 1)[0]

        found = {
            level: re.search(rf"`{level}`\s*→\s*(\S+)", emoji_section)
            for level in SEV_LEVELS
        }
        assert all(found.values()), f"unmapped levels: {[k for k, v in found.items() if not v]}"

        emojis = [m.group(1) for m in found.values()]
        assert len(set(emojis)) == len(emojis), f"severity emojis are not distinct: {emojis}"

    def test_report_has_a_defined_fallback_instead_of_guessing(self):
        emoji_section = REPORT_INSTRUCTION.split("## Severity Emojis", 1)[1].split("##", 1)[0]

        assert "never guess a level" in emoji_section

    @pytest.mark.parametrize("level", SEV_LEVELS)
    def test_synthesis_calibration_uses_the_sev_scale(self, level):
        calibration = _flat(SYNTHESIS_INSTRUCTION).split("**Severity calibration**", 1)[1]
        calibration = calibration.split("**Source agreement**", 1)[0]

        assert f"`{level}`" in calibration

    def test_synthesis_no_longer_requires_the_word_scale(self):
        """The old text: '"critical" requires: ...', '"high" requires: ...'."""
        calibration = _flat(SYNTHESIS_INSTRUCTION).split("**Severity calibration**", 1)[1]
        calibration = calibration.split("**Source agreement**", 1)[0]

        for word in WORD_SCALE:
            assert f'"{word}" requires' not in calibration

    def test_synthesis_assessment_task_uses_the_sev_scale(self):
        """Analysis Task 5 asked for critical/high/medium/low — a second mismatch."""
        flat = _flat(SYNTHESIS_INSTRUCTION)

        assert "Assess overall SEVERITY on the sev1-sev4 scale" in flat
        assert "Assess overall SEVERITY: critical / high / medium / low" not in flat

    def test_classifier_keeps_the_word_forms_as_input_hints_only(self):
        """Alerts DO carry 'P1'/'critical'. The classifier must still map them IN —
        it just must never emit them. Deleting the hints would break real inputs."""
        section = _severity_section(CLASSIFIER_INSTRUCTION)

        assert 'maps from "critical", "P1", "emergency"' in section
        assert 'maps from "low", "P4", "info"' in section

    def test_persistence_word_map_still_covers_a_stray_word_form(self):
        """Defence in depth. persistence normalises a word form if one ever appears;
        the classifier emitting sev-form must not make that mapping wrong."""
        from app.sub_agents.persistence import _SEV_MAP

        assert _SEV_MAP == {"critical": "sev1", "high": "sev2", "medium": "sev3", "low": "sev4"}


class TestNoBenchmarkSpecificTokensLeakIntoPrompts:
    """The guard ADO-200006 was believed to carry but does not.

    ADO-200006 (commit 1473d3a) added 26 tests across four files, none about prompt
    leakage. The nearest real match is ADO-200011's FORBIDDEN_IMPERATIVES in
    test_no_executetoolcode_meta_tools.py, which is about phantom tool calls.

    The constraint is real in spirit regardless: a prompt that names a ground-truth
    scenario's service is tuned to the benchmark, and the benchmark stops measuring
    anything. This test makes the guard actually exist.
    """

    # Tokens from ground-truth `tags` that predate this work as legitimate
    # CANONICALISATION counter-examples ("use `cloud-run`, NOT `recurringhighmem-us-c1`")
    # or as generic infrastructure vocabulary. Anything not on this list is a leak.
    ALLOWED = {
        # canonicalisation counter-examples, classifier.py service-name rules
        "sqlprod-c1",
        "cloud-run",
        # generic platform/product vocabulary, not scenario identifiers
        "hangfire",
        "mobile",
        "schedule",
        "calendar",
        "ios",
        "android",
        "deadlock",
        "feature-flag",
        "transient",
        # a GCP region, not a scenario token — synthesis uses it in a worked example
        # of an internal contradiction ("you assert us-west1, logging shows us-central1")
        "us-central1",
    }

    @staticmethod
    def _ground_truth_tokens() -> set[str]:
        root = pathlib.Path("tests/eval/ground-truth")
        tokens: set[str] = set()
        for path in sorted(root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            tokens.add(data["scenario_id"])
            tokens.update(data.get("tags", []))
        return tokens

    def test_the_ground_truth_corpus_is_actually_present(self):
        """A silently empty glob would make every assertion below vacuous."""
        tokens = self._ground_truth_tokens()

        assert len(tokens) > 20, f"ground-truth tokens not found (got {len(tokens)})"

    @pytest.mark.parametrize(
        "name",
        ["classifier", "report", "synthesis"],
    )
    def test_no_scenario_token_appears_in_the_prompt(self, name):
        prompt = {
            "classifier": CLASSIFIER_INSTRUCTION,
            "report": REPORT_INSTRUCTION,
            "synthesis": SYNTHESIS_INSTRUCTION,
        }[name]
        lowered = prompt.lower()
        leaked = sorted(
            token
            for token in self._ground_truth_tokens()
            if token.lower() not in self.ALLOWED and token.lower() in lowered
        )

        assert not leaked, (
            f"{name} prompt names benchmark-specific tokens {leaked} — tuning the prompt "
            "to the eval corpus makes the eval stop measuring anything"
        )

    def test_scenario_ids_never_appear_anywhere_in_a_prompt(self):
        """Strictest form: a scenario_id has no legitimate reason to be in a prompt."""
        root = pathlib.Path("tests/eval/ground-truth")
        ids = [json.loads(p.read_text(encoding="utf-8"))["scenario_id"] for p in root.glob("*.json")]

        for prompt in (CLASSIFIER_INSTRUCTION, REPORT_INSTRUCTION, SYNTHESIS_INSTRUCTION):
            for scenario_id in ids:
                assert scenario_id.lower() not in prompt.lower()

    def test_the_new_severity_rules_are_phrased_generally(self):
        """Named explicitly so a future edit cannot quietly reintroduce the shape."""
        section = _severity_section(CLASSIFIER_INSTRUCTION).lower()

        for token in ("octopus", "recurringhighmem", "apigateway", "startup probe", "startup-probe"):
            assert token not in section, f"severity rule names the benchmark scenario: {token}"


class TestCategoryAndServiceRulesAreUntouched:
    """AC #4 — the severity fix must not destabilise category (3/3) or service (3/3).

    Category carries a second-order blast radius severity lacks: TIER2_DISPATCH_MATRIX
    routes on it, and `cloud-infrastructure` maps to an empty agent list. A drift there
    changes which Tier-2 agents run. Severity changes one emoji and one stored field.
    """

    def test_category_disambiguation_rules_are_intact(self):
        flat = _flat(CLASSIFIER_INSTRUCTION)

        assert "**cloud-infrastructure vs deploy-release**" in flat
        assert "**application-errors vs business-logic**" in flat
        assert (
            "If Cloud Run startup probes fail AFTER a deploy, the issue is infrastructure"
            in flat
        )

    def test_service_canonicalisation_rules_are_intact(self):
        flat = _flat(CLASSIFIER_INSTRUCTION)

        assert 'Cloud Run services (any `*-us-c1`, `*-us-w1` suffix) → "cloud-run"' in flat
        assert 'SQL Server instances (`sqlprod*`, `sqlprod-c*`) → "WebCore"' in flat

    def test_new_examples_do_not_introduce_a_new_category(self):
        """Examples 6-8 must use categories the dispatch matrix already knows."""
        from app.categories import CATEGORIES

        examples = CLASSIFIER_INSTRUCTION.split("## Examples", 1)[1]
        used = set(re.findall(r'- category: "([a-z-]+)"', examples))

        unknown = used - set(CATEGORIES) - {"unknown"}
        assert not unknown, f"worked example uses an unknown category: {unknown}"

    def test_every_new_example_is_schema_complete(self):
        """A partial example teaches the model to omit fields."""
        examples = CLASSIFIER_INSTRUCTION.split("## Examples", 1)[1]
        blocks = re.findall(r"Classification:\n((?:- .+\n)+)", examples)

        assert len(blocks) >= 8, f"expected 8 worked examples, found {len(blocks)}"

        required = set(AlertClassification.model_fields)
        for i, block in enumerate(blocks, start=1):
            present = set(re.findall(r"- (\w+):", block))
            missing = required - present
            assert not missing, f"example {i} omits {sorted(missing)}"

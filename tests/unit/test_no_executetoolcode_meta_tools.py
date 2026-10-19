"""Regression tests for the phantom `executeToolCode` defect measured 2026-08-07.

Every one of 7 IR Agent sessions contained at least one:

    ValueError: Tool 'executeToolCode' not found. Available tools: fetch_deploy, ...

`executeToolCode` is a Bifrost meta-tool. It is NOT declared to the Tier-B agents at
all — each error's "Available tools" list was fully populated and matched the agent's
real ``tools=[...]`` exactly. The model invented the name, in every observed case
right after narrating a discovery sequence it never ran:

    The Output from allowed tools calls is - {"listToolFiles": ...}

Distribution of the 6 observed calls: deploy_correlation x4, datadog_observability x1,
gcp_metrics x1.

No evidence was lost — ``keep_running_on_tool_error`` turns the ValueError into a
non-fatal tool response, 4 of 6 retried with the real tool and got data. The cost is a
burned call slot: ~17% of the 6-call budget in TOKEN_BUDGET_RULES rule 1.

The fix is a prompt-level prohibition. These tests pin (a) that the prohibition is
present and names the real replacement tool on each affected agent, (b) that it is NOT
added to agents that legitimately use the meta-tools, and (c) that no affected prompt
still instructs the model to call one.
"""

import re

import pytest

from app.sub_agents.tier1.datadog_tierb import INSTRUCTION as DATADOG_INSTRUCTION
from app.sub_agents.tier1.datadog_tierb import datadog_tierb
from app.sub_agents.tier1.deploy_correlation_tierb import (
    INSTRUCTION as DEPLOY_INSTRUCTION,
)
from app.sub_agents.tier1.deploy_correlation_tierb import deploy_correlation_tierb
from app.sub_agents.tier1.gcp_metrics_tierb import (
    INSTRUCTION as GCP_METRICS_INSTRUCTION,
)
from app.sub_agents.tier1.gcp_metrics_tierb import gcp_metrics_tierb

# The Bifrost meta-tool surface. None of these is declared to a Tier-B agent.
META_TOOLS = ("executeToolCode", "listToolFiles", "getToolDocs", "readToolFile")

# (agent, prompt, real batch tool) for each agent that actually emitted the call live.
AFFECTED = [
    pytest.param(
        deploy_correlation_tierb, DEPLOY_INSTRUCTION, "fetch_deploy", id="deploy_correlation"
    ),
    pytest.param(
        datadog_tierb, DATADOG_INSTRUCTION, "fetch_datadog", id="datadog_observability"
    ),
    pytest.param(
        gcp_metrics_tierb, GCP_METRICS_INSTRUCTION, "fetch_gcp_metrics", id="gcp_metrics"
    ),
]


def _flat(text: str) -> str:
    """Collapse prompt line-wrapping so a prose assertion is not a formatting test."""
    return " ".join(text.split())


def _declared_tool_names(agent) -> set[str]:
    names = set()
    for tool in agent.tools:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        if name:
            names.add(name)
    return names


class TestTheHallucinatedToolWasNeverDeclared:
    """The premise of the fix: these agents never had the meta-tools to begin with.

    If this ever fails, the prohibition is wrong and must be removed rather than kept —
    telling a model it cannot use a tool it genuinely holds would destroy real capability.
    """

    @pytest.mark.parametrize("agent,_prompt,_fetch", AFFECTED)
    def test_no_meta_tool_is_declared(self, agent, _prompt, _fetch):
        declared = _declared_tool_names(agent)

        assert not declared & set(META_TOOLS), (
            f"{agent.name} declares a Bifrost meta-tool: {declared & set(META_TOOLS)}"
        )

    @pytest.mark.parametrize("agent,_prompt,fetch", AFFECTED)
    def test_the_real_batch_tool_is_the_replacement(self, agent, _prompt, fetch):
        """The prohibition points at this tool, so it had better exist."""
        assert fetch in _declared_tool_names(agent)


class TestEachAffectedPromptCarriesTheProhibition:
    @pytest.mark.parametrize("_agent,prompt,_fetch", AFFECTED)
    def test_prompt_names_every_meta_tool_as_unavailable(self, _agent, prompt, _fetch):
        header = "YOU HAVE NO BIFROST DISCOVERY META-TOOLS"
        assert header in prompt

        section = _flat(prompt).split(header, 1)[1]
        for meta in META_TOOLS:
            assert f"`{meta}`" in section, f"{meta} not named in the prohibition"
        assert "are NOT declared to" in section

    @pytest.mark.parametrize("_agent,prompt,fetch", AFFECTED)
    def test_prohibition_redirects_to_the_agents_own_batch_tool(self, _agent, prompt, fetch):
        """A bare 'do not' with no alternative is what produced the workaround."""
        section = _flat(prompt).split("YOU HAVE NO BIFROST DISCOVERY META-TOOLS", 1)[1]

        assert fetch in section
        assert f"`{fetch}` is your only tool" in section or f"`{fetch}` already runs" in section
        assert "call it directly" in section

    @pytest.mark.parametrize("_agent,prompt,_fetch", AFFECTED)
    def test_prohibition_forbids_narrating_a_discovery_sequence(self, _agent, prompt, _fetch):
        """The observed trigger: fabricated narration precedes the phantom call."""
        section = _flat(prompt).split("YOU HAVE NO BIFROST DISCOVERY META-TOOLS", 1)[1]

        assert "never narrate the output of a discovery step you did not take" in section
        assert '{"listToolFiles"' in section, "the measured narration form is not quoted"

    @pytest.mark.parametrize("_agent,prompt,_fetch", AFFECTED)
    def test_prohibition_states_the_measured_cost(self, _agent, prompt, _fetch):
        """These prompts justify rules with evidence; a bare prohibition gets ignored."""
        section = _flat(prompt).split("YOU HAVE NO BIFROST DISCOVERY META-TOOLS", 1)[1]

        assert "2026-08-07" in section
        assert "6-call" in section
        assert "17%" in section


class TestNoAffectedPromptStillTellsTheModelToCallOne:
    """The datadog prompt cites getToolDocs/readToolFile as an evidence SOURCE.

    That is fine as provenance, but an imperative ("call getToolDocs for the parameter
    names") is a direct instruction to invoke a tool the agent does not have — the
    unconstrained-window warning carried exactly that, contradicting the new rule.
    """

    FORBIDDEN_IMPERATIVES = (
        "call getToolDocs",
        "call listToolFiles",
        "call readToolFile",
        "call executeToolCode",
        "use executeToolCode",
        "via executeToolCode",
    )

    @pytest.mark.parametrize("_agent,prompt,_fetch", AFFECTED)
    def test_prompt_has_no_imperative_to_call_a_meta_tool(self, _agent, prompt, _fetch):
        lowered = prompt.lower()
        offenders = [p for p in self.FORBIDDEN_IMPERATIVES if p.lower() in lowered]

        assert not offenders, f"prompt still instructs a meta-tool call: {offenders}"

    def test_runtime_window_warning_no_longer_points_at_gettooldocs(self):
        """Verbatim string from the live warning path, not just the static prompt."""
        import inspect

        from app.sub_agents.tier1 import datadog_tierb as mod
        from app.sub_agents.tier1.datadog_tierb import fetch_datadog  # noqa: F401

        src = inspect.getsource(mod.fetch_datadog)

        assert "call getToolDocs for the exact parameter names" not in src


class TestAgentsThatLegitimatelyUseMetaToolsAreUntouched:
    """The prohibition must NOT spread to agents that really do hold the meta-tools.

    sentry_tierb and known_issues_tierb were candidates — both declare a single
    FunctionTool like the three affected agents — but neither emitted the phantom call
    in the 7 measured sessions, so neither gets the rule. The agents still wired through
    ``build_tool_list`` (teams_context, compute_engine_health, all of Tier 2, synthesis)
    genuinely receive executeToolCode and must keep their instructions to use it.
    """

    def test_teams_context_still_holds_the_meta_tools(self):

        # Bifrost is an McpToolset, resolved at runtime, so assert on the wiring
        # rather than on resolved tool names.
        import inspect

        from app.sub_agents.tier1 import teams_context

        assert "build_tool_list" in inspect.getsource(teams_context)

    @pytest.mark.parametrize(
        "module_name",
        ["sentry_tierb", "known_issues_tierb", "gcp_logging_tierb", "cloud_run_health_tierb"],
    )
    def test_unaffected_tierb_agents_did_not_get_the_rule(self, module_name):
        """Scope discipline: only the three agents with measured errors were changed."""
        import importlib

        mod = importlib.import_module(f"app.sub_agents.tier1.{module_name}")

        assert "YOU HAVE NO BIFROST DISCOVERY META-TOOLS" not in mod.INSTRUCTION

    def test_shared_meta_tool_budget_rule_is_unchanged(self):
        """TOKEN_BUDGET_RULES rule 6 is for the build_tool_list agents, not Tier-B.

        No Tier-B module imports shared_prompts, so this text never reaches an agent
        that lacks the meta-tools. Removing it would break the ones that have them.
        """
        from app.shared_prompts import TOKEN_BUDGET_RULES

        assert "Bifrost meta-tool budget" in TOKEN_BUDGET_RULES

    def test_no_tierb_module_imports_shared_prompts(self):
        """The premise of the test above — pin it so the isolation stays true."""
        import pathlib

        offenders = [
            p.name
            for p in pathlib.Path("app/sub_agents/tier1").glob("*_tierb.py")
            if "shared_prompts" in p.read_text(encoding="utf-8")
        ]

        assert not offenders, f"Tier-B agents now inherit the meta-tool budget: {offenders}"


class TestResilienceCallbackDocstringIsAccurate:
    """The callback stays; only its wrong attribution was corrected.

    It blamed an EMPTY tool list. In all 6 measured cases the list was fully populated —
    the model hallucinated a name. Both causes exist; the docstring must name both.
    """

    def test_docstring_records_the_populated_list_cause(self):
        from app.tools import tool_resilience

        doc = tool_resilience.__doc__

        assert "POPULATED tool list, hallucinated tool name" in doc
        assert "executeToolCode" in doc
        # The specific work item is not the point; that the docstring cites one is.
        assert re.search(r"ADO-\d+", doc), doc

    def test_docstring_keeps_the_original_empty_list_cause(self):
        """The 2026-06-25 empty-list crash was real and still possible."""
        from app.tools import tool_resilience

        doc = tool_resilience.__doc__

        assert "2026-06-25" in doc
        assert "Available tools: (empty)" in doc

    def test_callback_behavior_is_unchanged(self):
        """Comment-only edit: it must still return a dict so the run continues."""
        from app.tools.tool_resilience import keep_running_on_tool_error

        class _Tool:
            name = "executeToolCode"

        result = keep_running_on_tool_error(
            _Tool(), {}, None, ValueError("Tool 'executeToolCode' not found.")
        )

        assert isinstance(result, dict)
        assert result["status"] == "tool_unavailable"
        assert result["tool"] == "executeToolCode"

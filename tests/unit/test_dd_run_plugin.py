"""Per-sub-agent cost accounting, and the guarantee that it cannot break a run.

Concurrency is the case worth pinning: a single-slot accumulator merges two
agents' token counts into one wrong number.
"""

from __future__ import annotations

import asyncio
from unittest import mock

from app.app_utils import dd_run_plugin
from app.app_utils.bifrost_usage import RUN_TOTAL, Usage
from app.app_utils.dd_run_plugin import DatadogRunPlugin, _cost_usd, _new_usage


class FakeSpan:
    def __init__(self):
        self.metrics = {}
        self.tags = {}
        self.finished = False
        self.service = "ir-agent"

    def set_metric(self, key, value):
        self.metrics[key] = value

    def set_tag(self, key, value):
        self.tags[key] = value

    def get_tag(self, key):
        return self.tags.get(key)

    def set_exc_info(self, *args):
        self.error = args

    def finish(self):
        self.finished = True


class FakeContext:
    def __init__(self, invocation_id, agent_name, state=None):
        self.invocation_id = invocation_id
        self.agent_name = agent_name
        self.state = state or {}


class FakeAgent:
    def __init__(self, name, parent_agent=None):
        self.name = name
        self.parent_agent = parent_agent


class FakeUsage:
    def __init__(self, prompt, completion):
        self.prompt_token_count = prompt
        self.candidates_token_count = completion
        self.total_token_count = prompt + completion


class FakeResponse:
    def __init__(self, prompt, completion, model="claude-opus-5"):
        self.usage_metadata = FakeUsage(prompt, completion)
        self.model_version = model


class TestUsageAccounting:
    def test_two_sub_agents_do_not_merge_their_tokens(self):
        """tier1_parallel runs children concurrently."""
        plugin = DatadogRunPlugin()
        a = FakeContext("inv1", "sentry_agent")
        b = FakeContext("inv1", "datadog_agent")

        asyncio.run(plugin.after_model_callback(
            callback_context=a, llm_response=FakeResponse(100, 10)))
        asyncio.run(plugin.after_model_callback(
            callback_context=b, llm_response=FakeResponse(700, 70)))

        assert plugin._usage[("inv1", "sentry_agent")]["total"] == 110
        assert plugin._usage[("inv1", "datadog_agent")]["total"] == 770

    def test_two_invocations_do_not_merge(self):
        """One engine serves sessions concurrently."""
        plugin = DatadogRunPlugin()
        asyncio.run(plugin.after_model_callback(
            callback_context=FakeContext("inv1", "report"),
            llm_response=FakeResponse(100, 10)))
        asyncio.run(plugin.after_model_callback(
            callback_context=FakeContext("inv2", "report"),
            llm_response=FakeResponse(700, 70)))

        assert plugin._usage[("inv1", "report")]["total"] == 110
        assert plugin._usage[("inv2", "report")]["total"] == 770

    def test_repeated_calls_sum_rather_than_overwrite(self):
        """A sub-agent makes many model calls per run."""
        plugin = DatadogRunPlugin()
        ctx = FakeContext("inv1", "report")
        for _ in range(3):
            asyncio.run(plugin.after_model_callback(
                callback_context=ctx, llm_response=FakeResponse(100, 10)))
        assert plugin._usage[("inv1", "report")]["total"] == 330

    def test_run_total_accumulates_across_sub_agents(self):
        plugin = DatadogRunPlugin()
        asyncio.run(plugin.after_model_callback(
            callback_context=FakeContext("inv1", "sentry_agent"),
            llm_response=FakeResponse(100, 10)))
        asyncio.run(plugin.after_model_callback(
            callback_context=FakeContext("inv1", "datadog_agent"),
            llm_response=FakeResponse(700, 70)))
        assert plugin._run_usage["inv1"]["total"] == 880

    def test_response_without_usage_metadata_is_ignored(self):
        plugin = DatadogRunPlugin()
        response = mock.Mock()
        response.usage_metadata = None
        asyncio.run(plugin.after_model_callback(
            callback_context=FakeContext("inv1", "report"), llm_response=response))
        assert plugin._usage == {}


class TestRunIdentityOnSubAgentSpans:
    """Datadog span tags do not inherit parent to child, so the run identity
    has to be written onto each ir.sub_agent span directly."""

    def _open_span(self, root, invocation_id="inv1", agent_name="report"):
        plugin = DatadogRunPlugin()
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch("ddtrace.trace.tracer.start_span",
                           return_value=FakeSpan()), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"):
            asyncio.run(plugin.before_agent_callback(
                agent=FakeAgent(agent_name),
                callback_context=FakeContext(invocation_id, agent_name)))
        return plugin, plugin._spans[(invocation_id, agent_name)]

    def _close_span(self, plugin, root, invocation_id="inv1", agent_name="report"):
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent(agent_name, parent_agent=FakeAgent("root")),
                callback_context=FakeContext(invocation_id, agent_name)))

    def test_invocation_id_is_on_the_sub_agent_span_at_open(self):
        """The property that must hold: present on EVERY sub-agent span."""
        root = FakeSpan()
        _, span = self._open_span(root)
        assert span.tags["iragent.invocation_id"] == "inv1"

    def test_incident_id_is_copied_from_the_root_at_close(self):
        root = FakeSpan()
        root.tags["iragent.incident_id"] = "20260826-120000-deploy"
        plugin, span = self._open_span(root)
        self._close_span(plugin, root)
        assert span.tags["iragent.incident_id"] == "20260826-120000-deploy"

    def test_a_span_closing_before_incident_id_exists_still_has_invocation_id(self):
        """persistence.py mints incident_id late; early spans join via the root."""
        root = FakeSpan()
        plugin, span = self._open_span(root)
        self._close_span(plugin, root)
        assert "iragent.incident_id" not in span.tags
        assert span.tags["iragent.invocation_id"] == "inv1"

    def test_concurrent_sub_agents_get_their_own_invocation_id(self):
        root = FakeSpan()
        _, a = self._open_span(root, agent_name="sentry_agent")
        _, b = self._open_span(root, "inv2", "datadog_agent")
        assert a.tags["iragent.invocation_id"] == "inv1"
        assert b.tags["iragent.invocation_id"] == "inv2"

    def test_a_failed_sub_agent_span_also_carries_the_identity(self):
        root = FakeSpan()
        root.tags["iragent.incident_id"] = "inc-1"
        plugin, span = self._open_span(root)
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"):
            asyncio.run(plugin.on_agent_error_callback(
                agent=FakeAgent("report", parent_agent=FakeAgent("root")),
                callback_context=FakeContext("inv1", "report"),
                error=RuntimeError("boom")))
        assert span.tags["iragent.invocation_id"] == "inv1"
        assert span.tags["iragent.incident_id"] == "inc-1"

    def test_a_missing_root_does_not_break_the_close(self):
        root = FakeSpan()
        plugin, span = self._open_span(root)
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=None), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("report", parent_agent=FakeAgent("root")),
                callback_context=FakeContext("inv1", "report")))
        assert span.finished


class TestToolCalls:
    """Per-sub-agent tool counts. tier1_parallel runs children concurrently, so
    a naive counter merges them."""

    def test_each_sub_agent_counts_its_own_calls(self):
        plugin = DatadogRunPlugin()
        for _ in range(3):
            asyncio.run(plugin.before_tool_callback(
                tool=object(), tool_args={},
                tool_context=FakeContext("inv1", "sentry_agent")))
        asyncio.run(plugin.before_tool_callback(
            tool=object(), tool_args={},
            tool_context=FakeContext("inv1", "datadog_agent")))

        assert plugin._tool_calls[("inv1", "sentry_agent")] == 3
        assert plugin._tool_calls[("inv1", "datadog_agent")] == 1

    def test_two_invocations_do_not_merge(self):
        plugin = DatadogRunPlugin()
        asyncio.run(plugin.before_tool_callback(
            tool=object(), tool_args={},
            tool_context=FakeContext("inv1", "report")))
        asyncio.run(plugin.before_tool_callback(
            tool=object(), tool_args={},
            tool_context=FakeContext("inv2", "report")))

        assert plugin._tool_calls[("inv1", "report")] == 1
        assert plugin._tool_calls[("inv2", "report")] == 1
        assert plugin._run_tool_calls == {"inv1": 1, "inv2": 1}

    def test_the_count_lands_on_the_sub_agent_span(self):
        plugin = DatadogRunPlugin()
        span = FakeSpan()
        plugin._spans[("inv1", "report")] = span
        plugin._usage[("inv1", "report")] = _new_usage()
        asyncio.run(plugin.before_tool_callback(
            tool=object(), tool_args={},
            tool_context=FakeContext("inv1", "report")))
        asyncio.run(plugin.after_agent_callback(
            agent=FakeAgent("report", parent_agent=FakeAgent("root")),
            callback_context=FakeContext("inv1", "report")))

        assert span.metrics["iragent.tool_calls"] == 1.0

    def test_a_sub_agent_with_no_tools_reports_zero(self):
        """Zero is a real observation here, unlike an unpriced model."""
        plugin = DatadogRunPlugin()
        span = FakeSpan()
        plugin._spans[("inv1", "report")] = span
        plugin._usage[("inv1", "report")] = _new_usage()
        asyncio.run(plugin.after_agent_callback(
            agent=FakeAgent("report", parent_agent=FakeAgent("root")),
            callback_context=FakeContext("inv1", "report")))

        assert span.metrics["iragent.tool_calls"] == 0.0

    def test_the_run_total_rolls_up_onto_the_root_span(self):
        plugin = DatadogRunPlugin()
        dd_run_plugin._run_invocation.set("inv1")
        root_span = FakeSpan()
        plugin._run_usage["inv1"] = _new_usage()
        for name in ("sentry_agent", "sentry_agent", "datadog_agent"):
            asyncio.run(plugin.before_tool_callback(
                tool=object(), tool_args={},
                tool_context=FakeContext("inv1", name)))

        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root_span), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"), \
                mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value=None):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("incident_response_orchestrator"),
                callback_context=FakeContext("inv1", "incident_response_orchestrator")))

        assert root_span.metrics["iragent.tool_calls"] == 3.0
        assert "inv1" not in plugin._run_tool_calls

    def test_a_broken_tool_context_does_not_break_the_run(self):
        plugin = DatadogRunPlugin()
        broken = mock.Mock()
        type(broken).invocation_id = mock.PropertyMock(
            side_effect=RuntimeError("boom"))
        asyncio.run(plugin.before_tool_callback(
            tool=object(), tool_args={}, tool_context=broken))


class TestCost:
    def test_unpriced_model_yields_none_not_zero(self):
        """Zero is a number a dashboard sums into a wrong total."""
        assert _cost_usd({**_new_usage(), "total": 100, "model": ""}) is None
        assert _cost_usd(_new_usage()) is None

    def test_a_raising_price_table_yields_none(self):
        usage = {"prompt": 10, "completion": 5, "total": 15, "model": "x"}
        with mock.patch("litellm.cost_per_token", side_effect=RuntimeError("boom")):
            assert _cost_usd(usage) is None


class TestNeverRaises:
    def test_before_agent_survives_a_broken_tracer(self):
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        side_effect=RuntimeError("boom")):
            asyncio.run(DatadogRunPlugin().before_agent_callback(
                agent=FakeAgent("x"), callback_context=FakeContext("inv1", "x")))

    def test_after_agent_finishes_the_span_and_writes_metrics(self):
        plugin = DatadogRunPlugin()
        span = FakeSpan()
        plugin._spans[("inv1", "report")] = span
        plugin._usage[("inv1", "report")] = {
            "prompt": 100, "completion": 10, "total": 110, "model": "",
        }
        asyncio.run(plugin.after_agent_callback(
            agent=FakeAgent("report", parent_agent=FakeAgent("root")),
            callback_context=FakeContext("inv1", "report")))

        assert span.finished
        assert span.metrics["iragent.tokens_est"] == 110.0
        assert span.metrics["iragent.input_tokens_est"] == 100.0
        # Unpriced model -- no metric rather than a zero.
        assert "iragent.cost_usd_est" not in span.metrics
        # A sub-agent span never carries a billed figure; Bifrost cannot split
        # one gateway request across concurrent sub-agents.
        assert "iragent.cost_usd" not in span.metrics
        assert "iragent.tokens" not in span.metrics

    def test_root_agent_rolls_the_run_total_onto_the_root_span(self):
        """Every run must report a total, not just per-sub-agent numbers."""
        plugin = DatadogRunPlugin()
        dd_run_plugin._run_invocation.set("inv1")
        root_span = FakeSpan()
        plugin._run_usage["inv1"] = {
            "prompt": 800, "completion": 80, "total": 880, "model": "",
        }
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root_span),                 mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"),                 mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value=None):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("incident_response_orchestrator"),
                callback_context=FakeContext("inv1", "incident_response_orchestrator")))

        assert root_span.metrics["iragent.tokens_est"] == 880.0
        assert root_span.metrics["iragent.input_tokens_est"] == 800.0
        assert root_span.metrics["iragent.output_tokens_est"] == 80.0
        assert "inv1" not in plugin._run_usage


class TestBilledRunCost:
    """The run total is Bifrost's billed figure; the estimate is the fallback.

    A dashboard showing dollars must be able to tell which it is looking at.
    """

    def _run(self, *, billed):
        billed = None if billed is None else {RUN_TOTAL: billed}
        plugin = DatadogRunPlugin()
        dd_run_plugin._run_invocation.set("inv1")
        root_span = FakeSpan()
        plugin._run_usage["inv1"] = {
            "prompt": 800, "completion": 80, "total": 880, "model": "gpt-x",
        }
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root_span),                 mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"),                 mock.patch.object(dd_run_plugin, "_cost_usd", return_value=0.25),                 mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value=billed) as lookup:
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("incident_response_orchestrator"),
                callback_context=FakeContext("inv1", "incident_response_orchestrator")))
        return root_span, lookup

    def test_billed_cost_and_tokens_come_from_bifrost(self):
        span, _ = self._run(billed=Usage(cost_usd=1.75, tokens=900, records=14))
        assert span.metrics["iragent.cost_usd"] == 1.75
        assert span.metrics["iragent.tokens"] == 900.0
        assert span.metrics["iragent.billed_records"] == 14.0
        assert span.tags["iragent.cost_source"] == "bifrost"

    def test_unreachable_bifrost_leaves_BOTH_billed_metrics_ABSENT(self):
        """A blank cell is honest; a list-price number sums into a wrong total.

        Tokens matter as much as cost here -- ADO-200025 measured the estimate
        ~57% off on tokens against ~15% on cost.
        """
        span, _ = self._run(billed=None)
        assert "iragent.cost_usd" not in span.metrics
        assert "iragent.tokens" not in span.metrics
        assert span.tags["iragent.cost_source"] == "estimate_only"

    def test_the_estimate_is_still_recorded_under_its_own_names(self):
        """No fallback does not mean no number -- it means no MISLABELLED number."""
        span, _ = self._run(billed=None)
        assert span.metrics["iragent.cost_usd_est"] == 0.25
        assert span.metrics["iragent.tokens_est"] == 880.0

    def test_billed_and_estimated_never_share_a_metric_name(self):
        """Both present on the billed path, in separate series."""
        span, _ = self._run(billed=Usage(cost_usd=1.75, tokens=900, records=14))
        assert span.metrics["iragent.cost_usd"] == 1.75
        assert span.metrics["iragent.cost_usd_est"] == 0.25
        assert span.metrics["iragent.tokens"] == 900.0
        assert span.metrics["iragent.tokens_est"] == 880.0

    def test_the_lookup_runs_once_per_run_not_once_per_sub_agent(self):
        _, lookup = self._run(billed=Usage(cost_usd=1.0, tokens=1, records=1))
        assert lookup.call_count == 1

    def test_a_raising_lookup_does_not_break_the_run(self):
        plugin = DatadogRunPlugin()
        root_span = FakeSpan()
        plugin._run_usage["inv1"] = _new_usage()
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root_span),                 mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"),                 mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  side_effect=RuntimeError("boom")):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("incident_response_orchestrator"),
                callback_context=FakeContext("inv1", "incident_response_orchestrator")))
        assert root_span.tags["iragent.cost_source"] == "estimate_only"
        assert "iragent.cost_usd" not in root_span.metrics
        assert "iragent.tokens" not in root_span.metrics

    def test_agent_error_finishes_the_span_and_marks_the_run_failed(self):
        plugin = DatadogRunPlugin()
        span = mock.Mock()
        plugin._spans[("inv1", "report")] = span
        error = RuntimeError("boom")
        with mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span") as tag:
            asyncio.run(plugin.on_agent_error_callback(
                agent=FakeAgent("report"),
                callback_context=FakeContext("inv1", "report"),
                error=error))

        span.finish.assert_called_once()
        tag.assert_called_once_with(outcome="failed")


class TestRunRootPredicate:
    """Tier 2 agents are AgentTool-wrapped: parent_agent is None for them too,
    and AgentTool mints a fresh invocation_id."""

    def setup_method(self):
        dd_run_plugin._run_invocation.set("")

    def _claim_all(self, plugin, agents):
        """Drive several before_agent_callbacks inside ONE task.

        asyncio.run copies the context per call, so a set() in one would be
        invisible to the next. ADK runs the whole agent tree in a single
        create_task (runners.py:572), so one task is the faithful shape.
        """
        async def drive():
            for invocation_id, agent_name in agents:
                await plugin.before_agent_callback(
                    agent=FakeAgent(agent_name),
                    callback_context=FakeContext(invocation_id, agent_name))
            return dd_run_plugin._run_invocation.get(), dd_run_plugin.bf_headers.headers()

        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=None), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"):
            return asyncio.run(drive())

    def test_the_first_agent_claims_the_run_invocation_id(self):
        run_id, _ = self._claim_all(DatadogRunPlugin(), [("inv1", "orchestrator")])
        assert run_id == "inv1"

    def test_a_tier2_agent_does_not_overwrite_the_run_id(self):
        run_id, _ = self._claim_all(DatadogRunPlugin(), [
            ("inv1", "orchestrator"),
            ("e-fresh-uuid", "sql_deep_dive_agent"),
        ])
        assert run_id == "inv1"

    def test_tier2_agents_carry_the_runs_id_in_the_billing_header(self):
        """Defect (a): a fresh id here means the gateway record is dropped."""
        _, headers = self._claim_all(DatadogRunPlugin(), [
            ("inv1", "orchestrator"),
            ("e-fresh-uuid", "sql_deep_dive_agent"),
        ])
        assert headers["x-bf-lh-invocation_id"] == "inv1"
        assert headers["x-bf-lh-sub_agent"] == "sql_deep_dive_agent"

    def test_a_tier2_agent_is_not_the_run_root(self):
        """parent_agent is None for it, so the old predicate fired here."""
        plugin = DatadogRunPlugin()
        dd_run_plugin._run_invocation.set("inv1")
        assert not plugin._is_run_root(
            FakeAgent("sql_deep_dive_agent"),
            FakeContext("e-fresh-uuid", "sql_deep_dive_agent"))

    def test_a_sequential_child_is_not_the_run_root(self):
        """It shares the run's invocation_id, so the id check alone is not enough."""
        plugin = DatadogRunPlugin()
        dd_run_plugin._run_invocation.set("inv1")
        assert not plugin._is_run_root(
            FakeAgent("report", parent_agent=FakeAgent("orchestrator")),
            FakeContext("inv1", "report"))

    def test_the_orchestrator_is_the_run_root(self):
        plugin = DatadogRunPlugin()
        dd_run_plugin._run_invocation.set("inv1")
        assert plugin._is_run_root(
            FakeAgent("orchestrator"), FakeContext("inv1", "orchestrator"))


class TestPerSubAgentBilledCost:
    def setup_method(self):
        dd_run_plugin._run_invocation.set("")

    def test_billed_figures_land_on_the_retained_sub_agent_spans(self):
        plugin = DatadogRunPlugin()
        root = FakeSpan()
        sentry, datadog = FakeSpan(), FakeSpan()
        plugin._finished[("inv1", "sentry_agent")] = sentry
        plugin._finished[("inv1", "datadog_agent")] = datadog
        plugin._run_usage["inv1"] = _new_usage()
        dd_run_plugin._run_invocation.set("inv1")

        billed = {
            RUN_TOTAL: Usage(cost_usd=3.5, tokens=350, records=3),
            "sentry_agent": Usage(cost_usd=1.5, tokens=150, records=2),
            "datadog_agent": Usage(cost_usd=2.0, tokens=200, records=1),
        }
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"), \
                mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value=billed):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("orchestrator"),
                callback_context=FakeContext("inv1", "orchestrator")))

        assert sentry.metrics["iragent.cost_usd"] == 1.5
        assert sentry.tags["iragent.cost_source"] == "bifrost"
        assert datagog_cost(datadog) == 2.0
        assert root.metrics["iragent.cost_usd"] == 3.5

    def test_a_sub_agent_with_no_billed_records_is_tagged_estimate_only(self):
        plugin = DatadogRunPlugin()
        root = FakeSpan()
        quiet = FakeSpan()
        plugin._finished[("inv1", "quiet_agent")] = quiet
        plugin._run_usage["inv1"] = _new_usage()
        dd_run_plugin._run_invocation.set("inv1")

        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"), \
                mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value={RUN_TOTAL: Usage(1.0, 100, 1)}):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("orchestrator"),
                callback_context=FakeContext("inv1", "orchestrator")))

        assert "iragent.cost_usd" not in quiet.metrics
        assert quiet.tags["iragent.cost_source"] == "estimate_only"

    def test_writing_to_a_finished_span_is_what_this_relies_on(self):
        plugin = DatadogRunPlugin()
        root = FakeSpan()
        span = FakeSpan()
        span.finished = True
        plugin._finished[("inv1", "a")] = span
        plugin._run_usage["inv1"] = _new_usage()
        dd_run_plugin._run_invocation.set("inv1")

        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch.object(dd_run_plugin.dd_tags, "annotate_root_span"), \
                mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value={RUN_TOTAL: Usage(1.0, 10, 1),
                                                "a": Usage(1.0, 10, 1)}):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("orchestrator"),
                callback_context=FakeContext("inv1", "orchestrator")))

        assert span.metrics["iragent.cost_usd"] == 1.0


def datagog_cost(span):
    return span.metrics["iragent.cost_usd"]



class TestRunRootSpan:
    """The plugin must not depend on ddtrace's google-adk patch, which is
    disabled (DD_TRACE_GOOGLE_ADK_ENABLED=false) because it detaches an OTel
    context token across asyncio tasks once tier1_parallel fans out.

    Every scenario runs inside ONE task: ADK drives the whole agent tree from a
    single create_task (runners.py:572), and the ContextVar that carries the
    plugin-opened root only reaches tasks started after it is set.
    """

    def setup_method(self):
        dd_run_plugin._run_invocation.set("")
        dd_run_plugin.dd_tags.set_fallback_root(None)

    def _run(self, root, body, agents=(("inv1", "orchestrator"),)):
        """Open spans for `agents`, then await `body(plugin, opened)`."""
        plugin = DatadogRunPlugin()
        opened = []

        def start_span(name, **kwargs):
            span = FakeSpan()
            span.name = name
            span.kwargs = kwargs
            opened.append(span)
            return span

        async def drive():
            for invocation_id, agent_name in agents:
                await plugin.before_agent_callback(
                    agent=FakeAgent(agent_name),
                    callback_context=FakeContext(invocation_id, agent_name))
            await body(plugin, opened)

        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root),                 mock.patch("ddtrace.trace.tracer.start_span",
                           side_effect=start_span),                 mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value=None):
            asyncio.run(drive())
        return plugin, opened

    @staticmethod
    async def _nothing(plugin, opened):
        return None

    @staticmethod
    async def _finish_run(plugin, opened):
        await plugin.after_agent_callback(
            agent=FakeAgent("orchestrator"),
            callback_context=FakeContext("inv1", "orchestrator"))

    def test_no_ddtrace_root_means_the_plugin_opens_its_own(self):
        plugin, opened = self._run(None, self._nothing)

        run_root = opened[0]
        assert run_root.name == "ir.run"
        assert run_root.kwargs["service"] == "ir-agent"
        assert run_root.kwargs["activate"] is False
        assert "child_of" not in run_root.kwargs
        assert plugin._run_roots["inv1"] is run_root

    def test_the_sub_agent_span_hangs_off_the_root_the_plugin_opened(self):
        _, opened = self._run(None, self._nothing)

        run_root, sub = opened
        assert sub.name == "ir.sub_agent"
        assert sub.kwargs["child_of"] is run_root
        assert sub.tags["iragent.sub_agent"] == "orchestrator"

    def test_only_one_root_is_opened_for_the_whole_run(self):
        plugin, opened = self._run(None, self._nothing, agents=[
            ("inv1", "orchestrator"),
            ("inv1", "report"),
            ("e-fresh-uuid", "sql_deep_dive_agent"),
        ])
        assert [s.name for s in opened] == [
            "ir.run", "ir.sub_agent", "ir.sub_agent", "ir.sub_agent"]
        assert list(plugin._run_roots) == ["inv1"]

    def test_a_ddtrace_root_is_borrowed_rather_than_duplicated(self):
        root = FakeSpan()
        plugin, opened = self._run(root, self._nothing)

        assert [s.name for s in opened] == ["ir.sub_agent"]
        assert opened[0].kwargs["child_of"] is root
        assert plugin._run_roots == {}

    def test_run_tags_resolve_to_the_root_the_plugin_opened(self):
        """annotate_root_span is how persistence.py stamps incident_id."""
        async def tag(plugin, opened):
            dd_run_plugin.dd_tags.annotate_root_span(incident_id="INC-1")

        _, opened = self._run(None, tag)
        run_root = opened[0]
        assert run_root.tags["iragent.invocation_id"] == "inv1"
        assert run_root.tags["iragent.incident_id"] == "inc-1"

    def test_the_run_rollup_and_outcome_land_on_the_root_the_plugin_opened(self):
        async def finish(plugin, opened):
            plugin._run_usage["inv1"] = {
                "prompt": 800, "completion": 80, "total": 880, "model": "",
            }
            await self._finish_run(plugin, opened)

        plugin, opened = self._run(None, finish)
        run_root = opened[0]
        assert run_root.metrics["iragent.tokens_est"] == 880.0
        assert run_root.metrics["iragent.tool_calls"] == 0.0
        assert run_root.tags["iragent.outcome"] == "completed"
        assert run_root.finished
        assert plugin._run_roots == {}

    def test_the_incident_id_is_copied_onto_the_sub_agent_span(self):
        """persistence.py tags the root after the sub-agents have started."""
        async def finish(plugin, opened):
            opened[0].set_tag("iragent.incident_id", "inc-1")
            await self._finish_run(plugin, opened)

        _, opened = self._run(None, finish)
        assert opened[1].tags["iragent.incident_id"] == "inc-1"

    def test_a_failed_run_closes_the_root_the_plugin_opened(self):
        async def fail(plugin, opened):
            await plugin.on_agent_error_callback(
                agent=FakeAgent("orchestrator"),
                callback_context=FakeContext("inv1", "orchestrator"),
                error=RuntimeError("boom"))

        plugin, opened = self._run(None, fail)
        run_root = opened[0]
        assert run_root.tags["iragent.outcome"] == "failed"
        assert run_root.finished
        assert plugin._run_roots == {}

    def test_a_borrowed_root_is_never_finished_by_the_plugin(self):
        root = FakeSpan()
        plugin, _ = self._run(root, self._finish_run)
        assert not root.finished


class TestOrphanedSubAgentSpans:
    """A sub-agent whose after_agent_callback never fires leaves its span open.

    tier1_parallel gates a non-selected Tier 1 agent by returning Content from
    before_agent_callback; ADK sets end_invocation and returns before the after
    callbacks (google/adk/agents/base_agent.py:300-313). With
    DD_TRACE_PARTIAL_FLUSH_ENABLED=false one unfinished span holds back the
    whole trace, so the run root and every sibling never reach Datadog.
    """

    def setup_method(self):
        dd_run_plugin._run_invocation.set("inv1")
        dd_run_plugin.dd_tags.set_fallback_root(None)

    def _finish_run(self, plugin, root):
        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value=None):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("orchestrator"),
                callback_context=FakeContext("inv1", "orchestrator")))

    def test_a_skipped_sub_agents_span_is_finished_at_end_of_run(self):
        plugin = DatadogRunPlugin()
        skipped = FakeSpan()
        plugin._spans[("inv1", "sentry_tierb")] = skipped
        plugin._run_usage["inv1"] = _new_usage()

        self._finish_run(plugin, FakeSpan())

        assert skipped.finished
        assert skipped.tags["iragent.skipped"] == "true"
        assert plugin._spans == {}

    def test_a_skipped_span_keeps_the_tag_its_metric_filters_on(self):
        """The span-based metrics filter on @iragent.sub_agent:*."""
        plugin = DatadogRunPlugin()
        skipped = FakeSpan()
        skipped.set_tag("iragent.sub_agent", "sentry_tierb")
        plugin._spans[("inv1", "sentry_tierb")] = skipped
        plugin._run_usage["inv1"] = _new_usage()

        self._finish_run(plugin, FakeSpan())

        assert skipped.tags["iragent.sub_agent"] == "sentry_tierb"

    def test_a_skipped_span_still_receives_the_billed_lookup(self):
        plugin = DatadogRunPlugin()
        skipped = FakeSpan()
        plugin._spans[("inv1", "sentry_tierb")] = skipped
        plugin._run_usage["inv1"] = _new_usage()
        root = FakeSpan()

        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=root), \
                mock.patch.object(dd_run_plugin.bifrost_usage, "from_env",
                                  return_value={RUN_TOTAL: Usage(1.0, 10, 1)}):
            asyncio.run(plugin.after_agent_callback(
                agent=FakeAgent("orchestrator"),
                callback_context=FakeContext("inv1", "orchestrator")))

        assert skipped.tags["iragent.cost_source"] == "estimate_only"

    def test_a_sub_agent_that_did_close_normally_is_not_re_tagged(self):
        plugin = DatadogRunPlugin()
        normal = FakeSpan()
        plugin._spans[("inv1", "gcp_logging_tierb")] = normal
        plugin._usage[("inv1", "gcp_logging_tierb")] = _new_usage()
        plugin._run_usage["inv1"] = _new_usage()

        asyncio.run(plugin.after_agent_callback(
            agent=FakeAgent("gcp_logging_tierb", parent_agent=FakeAgent("t1")),
            callback_context=FakeContext("inv1", "gcp_logging_tierb")))
        self._finish_run(plugin, FakeSpan())

        assert normal.finished
        assert "iragent.skipped" not in normal.tags

    def test_a_tier2_span_under_a_fresh_invocation_id_is_closed_too(self):
        """AgentTool mints a new invocation_id for Tier 2 agents."""
        plugin = DatadogRunPlugin()
        orphan = FakeSpan()
        plugin._spans[("inv1", "sql_deep_dive_agent")] = orphan
        plugin._run_usage["inv1"] = _new_usage()

        self._finish_run(plugin, FakeSpan())

        assert orphan.finished

    def test_a_failed_run_also_closes_its_orphans(self):
        plugin = DatadogRunPlugin()
        orphan = FakeSpan()
        plugin._spans[("inv1", "sentry_tierb")] = orphan

        with mock.patch("ddtrace.trace.tracer.current_root_span",
                        return_value=FakeSpan()):
            asyncio.run(plugin.on_agent_error_callback(
                agent=FakeAgent("orchestrator"),
                callback_context=FakeContext("inv1", "orchestrator"),
                error=RuntimeError("boom")))

        assert orphan.finished
        assert plugin._spans == {}

    def test_another_sessions_spans_are_left_alone(self):
        """The engine serves sessions concurrently on one plugin instance."""
        plugin = DatadogRunPlugin()
        mine, theirs = FakeSpan(), FakeSpan()
        plugin._spans[("inv1", "sentry_tierb")] = mine
        plugin._spans[("inv2", "sentry_tierb")] = theirs
        plugin._run_usage["inv1"] = _new_usage()

        self._finish_run(plugin, FakeSpan())

        assert mine.finished
        assert not theirs.finished
        assert ("inv2", "sentry_tierb") in plugin._spans

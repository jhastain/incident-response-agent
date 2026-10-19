"""Regression tests for the two accuracy defects found in the 2026-07-27 live run.

Bug 1: Datadog time-scoped calls were issued with no time argument, so they silently
       returned the Datadog default window (~last hour = "now"). The report then
       narrated those numbers as the incident window, fabricating a pre-alert spike.

Bug 2: The verification agent scored citations without ever reading tool evidence —
       get_session_tool_outputs resolved a DELETED engine id, returned "session does
       not exist", and the verifier graded the report's prose anyway (9/9 VALID).
"""

import json
import os
from unittest import mock

import pytest

from app.sub_agents.tier1.datadog_tierb import (
    _ineffective_range_calls,
    _unconstrained_time_calls,
)
from app.tools.session_tools import (
    _evidence_unavailable,
    _resolve_engine_context,
    get_session_tool_outputs,
)

# The literal calls the agent made during the bad run.
FAILING_SPANS_CALL = (
    'result = datadog_token.aggregate_spans('
    'query="service:acme.example status:error", limit=10)\nprint(result)'
)
FAILING_LOGS_CALL = (
    'result = datadog_token.search_datadog_logs('
    'query="source:auth0 status:error", limit=10)\nprint(result)'
)


class TestUnconstrainedWindowDetection:
    def test_flags_the_exact_calls_that_fabricated_the_window(self):
        assert _unconstrained_time_calls(FAILING_SPANS_CALL) == ["aggregate_spans"]
        assert _unconstrained_time_calls(FAILING_LOGS_CALL) == ["search_datadog_logs"]

    def test_service_name_containing_end_does_not_mask_a_bad_call(self):
        """'acme.example' contains the substring 'end'.

        A naive substring check for time hints treated that as an `end=` argument and
        let the offending call through. Matching must be on whole kwarg tokens.
        """
        code = 'result = datadog_token.search_datadog_spans(query="service:acme.example")'
        assert _unconstrained_time_calls(code) == ["search_datadog_spans"]

    @pytest.mark.parametrize(
        "code",
        [
            # NOTE: an in-query @timestamp range is deliberately NOT in this list.
            # It was originally treated as a valid constraint; the 2026-07-27 e2e
            # proved the server ignores it. See TestInQueryTimestampIsNotAConstraint.
            #
            # `from` is a reserved word, so the only way to pass it is dict-unpacking.
            'args = {"query": "x", "from": "2026-07-27T08:02:00Z", '
            '"to": "2026-07-27T08:47:00Z"}\n'
            "result = datadog_token.search_datadog_logs(**args)",
            'args = {"queries": ["avg:system.cpu{*}"], "from": "now-1h", "to": "now"}\n'
            "result = datadog_token.get_datadog_metric(**args)",
            # A literal `to=` is legal (only `from` is reserved).
            'result = datadog_token.search_datadog_spans(query="x", to="2026-07-27T08:47:00Z")',
        ],
    )
    def test_accepts_constrained_calls(self, code):
        assert _unconstrained_time_calls(code) == []

    @pytest.mark.parametrize(
        "code",
        [
            # from_time=/to_time= is not a parameter of ANY Datadog tool. Live-measured
            # 2026-07-28 on aggregate_spans: baseline (no window) 435807, from_time/to_time
            # 435517, real from/to 467682 — it tracked the rolling default, i.e. dropped.
            'result = datadog_token.search_datadog_logs(query="x", '
            'from_time="2026-07-27T08:02:00Z", to_time="2026-07-27T08:47:00Z")',
            # start=/end= likewise: accepted and silently discarded by the span tools.
            'result = datadog_token.get_datadog_metric(query="avg:system.cpu{*}", start=1, end=2)',
            # A bare "now-1h" inside the QUERY STRING is a search term, not a bound.
            # Live-measured: aggregate_spans(query="service:acme.example now-1h")
            # returned total_buckets 0 — it matched nothing, it did not scope time.
            'result = datadog_token.search_datadog_logs(query="service:acme.example now-1h")',
        ],
    )
    def test_rejects_forms_the_server_silently_drops(self, code):
        """These LOOK constrained and are not — the most dangerous class of call.

        Accepting any of them suppresses the warning while the query runs against the
        rolling last hour, which is exactly the defect this guard exists to catch.
        """
        assert _unconstrained_time_calls(code) != []

    @pytest.mark.parametrize(
        "code",
        [
            'result = datadog_token.search_datadog_monitors(query="alert", limit=10)',
            'result = datadog_token.get_datadog_dashboard(dashboard_id="aaa-111-bbb")',
        ],
    )
    def test_ignores_tools_that_are_not_time_scoped(self, code):
        assert _unconstrained_time_calls(code) == []


class TestEngineContextResolution:
    # The real APP_URL injected into the deployed engine.
    REAL_APP_URL = (
        "https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1/projects/"
        "000000000002/locations/us-central1/reasoningEngines/1000000000000000002/api"
    )

    def test_resolves_running_engine_from_app_url(self):
        with mock.patch.dict(os.environ, {"APP_URL": self.REAL_APP_URL}, clear=True):
            assert _resolve_engine_context() == (
                "000000000002",
                "us-central1",
                "1000000000000000002",
            )

    def test_ignores_global_location_env_var(self):
        """The deployed engine sets GOOGLE_CLOUD_LOCATION=global.

        Using it builds a global-aiplatform.googleapis.com sessions URL, which is the
        wrong host. The location must come from APP_URL.
        """
        env = {"APP_URL": self.REAL_APP_URL, "GOOGLE_CLOUD_LOCATION": "global"}
        with mock.patch.dict(os.environ, env, clear=True):
            _, location, _ = _resolve_engine_context()
        assert location == "us-central1"

    def test_raises_rather_than_using_a_stale_engine_id(self):
        """No silent fallback.

        The previous default was the deleted engine 1000000000000000003, which made
        every verification run fail open instead of failing loudly.
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="cannot resolve the running engine"):
                _resolve_engine_context()


class TestEvidenceUnavailableSignal:
    def test_flags_unavailable_evidence_and_demands_unreliable(self):
        payload = json.loads(_evidence_unavailable("session does not exist"))
        assert payload["evidence_unavailable"] is True
        assert "UNRELIABLE" in payload["instruction_to_verifier"]
        assert "evidence_citations_valid=0" in payload["instruction_to_verifier"]


# --- Defects found by the 2026-07-27 post-deploy e2e audit -------------------


class TestInQueryTimestampIsNotAConstraint:
    """The MCP server DROPS an in-query @timestamp range.

    Live-verified: a query carrying @timestamp:[2026-07-21T20:14:26Z TO
    2026-07-21T20:59:26Z] returned a logs_explorer_url whose from_ts/to_ts were
    2026-07-27T17:21-18:21Z. Accepting that syntax as a constraint was worse than
    not detecting it: the query ran against "now" AND the warning was suppressed.
    """

    IN_QUERY_ONLY = (
        'result = datadog_token.search_datadog_spans(query="service:acme.example '
        '@timestamp:[2026-07-21T20:14:26Z TO 2026-07-21T20:59:26Z]", limit=10)'
    )

    def test_in_query_range_no_longer_suppresses_the_warning(self):
        assert _unconstrained_time_calls(self.IN_QUERY_ONLY) == ["search_datadog_spans"]

    def test_in_query_range_is_reported_as_ineffective(self):
        assert _ineffective_range_calls(self.IN_QUERY_ONLY) == ["search_datadog_spans"]

    def test_real_from_to_is_accepted_and_not_called_ineffective(self):
        code = (
            'args = {"query": "x", "from": "2026-07-21T20:14:26Z", '
            '"to": "2026-07-21T20:59:26Z"}\n'
            "result = datadog_token.search_datadog_logs(**args)"
        )
        assert _unconstrained_time_calls(code) == []
        assert _ineffective_range_calls(code) == []

    def test_relative_bound_counts_only_as_a_from_to_VALUE(self):
        """"now-1h" is a valid VALUE for `from`, never a bound inside the query text.

        Live-measured 2026-07-28: aggregate_spans(query="service:acme.example now-1h")
        returned total_buckets 0 — the server parsed it as a search term and matched
        nothing. The same window passed as {"from": "now-1h", "to": "now"} returned
        434,696. An in-query relative bound is not a constraint.
        """
        in_query = 'result = datadog_token.search_datadog_logs(query="service:x now-1h")'
        assert _unconstrained_time_calls(in_query) == ["search_datadog_logs"]

        as_value = (
            'args = {"query": "service:x", "from": "now-1h", "to": "now"}\n'
            "result = datadog_token.search_datadog_logs(**args)"
        )
        assert _unconstrained_time_calls(as_value) == []
        assert _ineffective_range_calls(as_value) == []


class TestVerifierResolvesItsOwnSessionId:
    """The verifier had no source for its session id and invented "current".

    Nothing injected a {session_id} into its prompt and include_contents="none"
    removed every other route, so every lookup 404'd. The gate then tripped on
    every run — which looked like a working gate but meant zero citations were
    ever actually validated.
    """

    class _Ctx:
        def __init__(self, session_id):
            self.session = type("S", (), {"id": session_id})()

    def _run(self, ctx, session_id=""):
        # Stop before any network call; we only care which id was resolved.
        with mock.patch(
            "app.tools.session_tools._resolve_engine_context",
            side_effect=RuntimeError("stop-after-resolution"),
        ):
            return json.loads(get_session_tool_outputs(ctx, session_id))

    def test_uses_the_real_session_id_from_tool_context(self):
        out = self._run(self._Ctx("5208215812875223040"))
        # Reached engine resolution, meaning a session id was accepted.
        assert "stop-after-resolution" in out["error"]

    @pytest.mark.parametrize("invented", ["current", "CURRENT", "unknown", "none"])
    def test_ignores_invented_placeholder_ids(self, invented):
        out = self._run(self._Ctx("5208215812875223040"), invented)
        assert "stop-after-resolution" in out["error"]

    def test_reports_evidence_unavailable_when_no_session_can_be_found(self):
        ctx = type("C", (), {"session": None})()
        out = json.loads(get_session_tool_outputs(ctx, ""))
        assert out["evidence_unavailable"] is True
        assert "session id" in out["error"]


class TestPermissionErrorsNameTheIdentity:
    """A 403 is a fact about WHICH identity is asking, not about the project.

    The engine was deployed without --service-account, so it ran as the shared
    Google-managed reasoning-engine service agent (viewer roles on the dev project
    only) instead of ir-agent-sa. 32 tool calls 403'd against acme-production-100000
    and the agent reported them as "expected; human investigators have access".
    """

    DENIED = Exception("403 Permission denied on resource project acme-production-100000")
    SHARED_SA = "service-000000000002@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
    OWN_SA = "ir-agent-sa@aiops-dev-project.iam.gserviceaccount.com"

    def _err(self, identity, exc=None):
        from app.tools import gcp_tools

        with mock.patch.object(gcp_tools, "_runtime_identity", return_value=identity):
            return json.loads(
                gcp_tools._permission_error(
                    "query_gcp_logs", "acme-production-100000", exc or self.DENIED
                )
            )

    def test_wrong_identity_is_called_a_deployment_defect(self):
        out = self._err(self.SHARED_SA)
        assert out["permission_denied"] is True
        assert out["runtime_identity"] == self.SHARED_SA
        assert "--service-account" in out["diagnosis"]
        assert "deployment defect" in out["diagnosis"]

    def test_correct_identity_points_at_a_missing_grant_instead(self):
        out = self._err(self.OWN_SA)
        assert out["permission_denied"] is True
        assert "grant is missing" in out["diagnosis"]
        assert "deployment defect" not in out["diagnosis"]

    def test_denial_forbids_substituting_an_invented_number(self):
        out = self._err(self.SHARED_SA)
        assert "NO DATA" in out["instruction"]
        assert "invented number is a defect" in out["instruction"]

    def test_non_permission_errors_pass_through_unchanged(self):
        out = self._err(self.OWN_SA, Exception("deadline exceeded"))
        assert sorted(out.keys()) == ["error"]

    def test_every_gcp_tool_routes_denials_through_the_diagnostic(self):
        import inspect

        from app.tools import gcp_tools

        src = inspect.getsource(gcp_tools)
        # 6 call sites + the definition itself
        assert src.count("_permission_error(") == 7
        assert 'json.dumps({"error": str(e)})' not in src


class TestNumbersAreQuotedRuleIsInEveryAgentPrompt:
    def test_rule_reaches_tier1_and_tier2_agents(self):
        from app.shared_prompts import build_agent_instruction

        instruction = build_agent_instruction(
            role="Test Agent",
            context_refs=["{classification}"],
            checklist=["do a thing"],
            tools_available=["query_gcp_logs"],
        )
        assert "Numbers Are Quoted, Not Estimated" in instruction
        assert "Ranges must be real" in instruction


class TestAlertTimestampExtraction:
    """The classifier returned alert_timestamp="unknown" and the whole run went to the wrong day.

    PagerDuty #1003's only machine-readable trigger time was `from_ts=1784665701000`
    inside a monitor URL. The classifier missed it, every query anchored on "now", and
    the agent produced a STRONG-EVIDENCE root cause for an incident six days later.
    Correct answer for that alert: one error at 2026-07-21T20:41:20Z.
    """

    def test_extracts_the_real_alert_that_failed(self):
        from app.sub_agents.classifier import extract_epoch_timestamp

        # Verbatim fragment of the PagerDuty #1003 monitor URL.
        text = (
            "monitor 1064301 (from_ts=1784665701000&to_ts=1784666901000"
            "&link_event_ts=1784666601)"
        )
        assert extract_epoch_timestamp(text) == "2026-07-21T20:28:21Z"

    def test_prefers_window_start_over_resolve_marks(self):
        from app.sub_agents.classifier import extract_epoch_timestamp

        # to_ts/link_event_ts are later; anchoring on them shifts the window.
        text = "from_ts=1784665701000&to_ts=1784666901000&link_event_ts=1784666601"
        assert extract_epoch_timestamp(text) == "2026-07-21T20:28:21Z"

    def test_handles_10_digit_seconds_as_well_as_13_digit_millis(self):
        from app.sub_agents.classifier import extract_epoch_timestamp

        assert extract_epoch_timestamp("event_ts=1784665701") == "2026-07-21T20:28:21Z"

    def test_returns_empty_when_no_epoch_present(self):
        from app.sub_agents.classifier import extract_epoch_timestamp

        assert extract_epoch_timestamp("CPU is high on prod, no timestamps here") == ""
        assert extract_epoch_timestamp("") == ""

    def test_verifier_hard_fails_on_unknown_timestamp(self):
        from app.sub_agents.verification import verification_agent

        instruction = verification_agent.instruction
        # An unknown anchor must be UNRELIABLE by construction, not a judgment call.
        assert "alert_timestamp" in instruction
        assert "UNRELIABLE" in instruction
        assert "This is not a judgment call" in instruction
        # And it must read the alert time from classification, not from the report.
        assert "{classification}" in instruction
        # CDT is UTC-5; the verifier previously asserted 1:28 PM CST == 13:28 UTC.
        assert "CDT is UTC−5" in instruction

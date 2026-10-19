"""Reading this run's billed cost back from Bifrost.

Two things carry the correctness of this module: the metadata filter (one
virtual key bills every run, and Agent Engine serves 9 sessions concurrently,
so a time window alone merges two live investigations) and the settle-poll
(Bifrost writes each record seconds after its own response, so returning on
first sight undercounts).
"""

from __future__ import annotations

import urllib.error
from unittest import mock

from app.app_utils import bifrost_usage
from app.app_utils.bifrost_usage import Usage, for_run


def _record(invocation_id, cost, tokens, agent=bifrost_usage.AGENT_NAME,
            sub_agent=None, status="success"):
    metadata = {"invocation_id": invocation_id, "agent": agent}
    if sub_agent is not None:
        metadata["sub_agent"] = sub_agent
    return {
        "cost": cost,
        "status": status,
        "token_usage": {"total_tokens": tokens},
        "metadata": metadata,
    }


def _page(records):
    return {"logs": records, "pagination": {"total_count": len(records)}}


class TestFiltering:
    def test_only_this_invocations_records_count(self):
        """A concurrent investigation's spend must not land on this run."""
        page = _page([
            _record("inv1", 1.0, 100),
            _record("inv2", 9.0, 900),
            _record("inv1", 0.5, 50),
        ])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage[bifrost_usage.RUN_TOTAL] == Usage(cost_usd=1.5, tokens=150, records=2)

    def test_another_agent_on_the_same_gateway_is_excluded(self):
        page = _page([_record("inv1", 5.0, 500, agent="bug-analyzer")])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage is None

    def test_no_matching_records_is_none_not_zero(self):
        """Zero would read as a free investigation on a cost dashboard."""
        with mock.patch.object(bifrost_usage, "_get_page", return_value=_page([])):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage is None


class TestSettlePolling:
    def test_polls_until_the_record_count_stops_growing(self):
        """Bifrost's write lag means the first read is short."""
        pages = [
            _page([_record("inv1", 1.0, 100)]),
            _page([_record("inv1", 1.0, 100), _record("inv1", 2.0, 200)]),
            _page([_record("inv1", 1.0, 100), _record("inv1", 2.0, 200)]),
        ]
        with mock.patch.object(bifrost_usage, "_get_page", side_effect=pages), \
                mock.patch.object(bifrost_usage.time, "sleep"):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=60)
        assert usage[bifrost_usage.RUN_TOTAL] == Usage(cost_usd=3.0, tokens=300, records=2)

    def test_a_never_settling_count_returns_the_partial_at_the_deadline(self):
        """Slightly-low beats absent -- the run still gets a figure."""
        growing = [
            _page([_record("inv1", 1.0, 100)] * n) for n in range(1, 12)
        ]
        with mock.patch.object(bifrost_usage, "_get_page", side_effect=growing), \
                mock.patch.object(bifrost_usage.time, "sleep"), \
                mock.patch.object(bifrost_usage.time, "monotonic",
                                  side_effect=[0, 0, 100, 200]):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=30)
        assert usage is not None
        assert usage[bifrost_usage.RUN_TOTAL].records >= 1


class TestNeverRaises:
    def test_an_unreachable_gateway_yields_none(self):
        with mock.patch.object(bifrost_usage, "_get_page",
                               side_effect=urllib.error.URLError("down")):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage is None

    def test_a_missing_credential_short_circuits(self):
        """ADO-200026: no read-scoped key exists yet, so this is the live path."""
        with mock.patch.object(bifrost_usage, "_get_page") as get_page:
            usage = for_run(base_url="https://gw", logs_key="",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage is None
        get_page.assert_not_called()

    def test_malformed_records_do_not_raise(self):
        page = _page([{"status": "success",
                       "metadata": {"invocation_id": "inv1",
                                    "agent": bifrost_usage.AGENT_NAME}}])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage[bifrost_usage.RUN_TOTAL] == Usage(cost_usd=0.0, tokens=0, records=1)


class TestEnvResolution:
    def test_base_url_is_derived_from_the_llm_url(self):
        """/api/logs sits beside /v1, not under it."""
        with mock.patch.dict("os.environ",
                             {"BIFROST_LLM_URL": "https://test.bifrost.example/v1",
                              "BIFROST_BASE_URL": ""}, clear=False):
            assert bifrost_usage.base_url_from_env() == "https://test.bifrost.example"

    def test_an_explicit_base_url_wins(self):
        with mock.patch.dict("os.environ",
                             {"BIFROST_LLM_URL": "https://a.example/v1",
                              "BIFROST_BASE_URL": "https://b.example"}, clear=False):
            assert bifrost_usage.base_url_from_env() == "https://b.example"

    def test_the_logs_key_is_not_the_virtual_key(self):
        """/api/logs takes a management-scoped key, not BIFROST_API_TOKEN."""
        with mock.patch.dict("os.environ",
                             {"BIFROST_API_TOKEN": "vk-secret",
                              "BIFROST_LOGS_API_KEY": ""}, clear=False):
            assert bifrost_usage.logs_key_from_env() == ""


class TestPerSubAgentGrouping:
    """One query for the run, grouped locally: /api/logs has no metadata filter."""

    def test_records_group_by_sub_agent_and_still_total(self):
        page = _page([
            _record("inv1", 1.0, 100, sub_agent="sentry_agent"),
            _record("inv1", 0.5, 50, sub_agent="sentry_agent"),
            _record("inv1", 2.0, 200, sub_agent="datadog_agent"),
        ])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)

        assert usage["sentry_agent"] == Usage(cost_usd=1.5, tokens=150, records=2)
        assert usage["datadog_agent"] == Usage(cost_usd=2.0, tokens=200, records=1)
        assert usage[bifrost_usage.RUN_TOTAL] == Usage(
            cost_usd=3.5, tokens=350, records=3)

    def test_a_record_with_no_sub_agent_lands_only_in_the_total(self):
        page = _page([_record("inv1", 1.0, 100)])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert set(usage) == {bifrost_usage.RUN_TOTAL}
        assert usage[bifrost_usage.RUN_TOTAL].records == 1

    def test_tier2_records_land_on_the_run_that_dispatched_them(self):
        """AgentTool mints a fresh invocation_id; the header carries the run's."""
        page = _page([
            _record("inv1", 1.0, 100, sub_agent="tier1_sentry"),
            _record("inv1", 4.0, 400, sub_agent="sql_deep_dive_agent"),
        ])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage["sql_deep_dive_agent"].cost_usd == 4.0
        assert usage[bifrost_usage.RUN_TOTAL].cost_usd == 5.0


class TestStatusFilter:
    def test_processing_records_are_ignored(self):
        """They carry cost 0 but would make the settle check finish early."""
        page = _page([
            _record("inv1", 1.0, 100, sub_agent="a"),
            _record("inv1", 0, 0, sub_agent="a", status="processing"),
        ])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage[bifrost_usage.RUN_TOTAL].records == 1
        assert usage[bifrost_usage.RUN_TOTAL].cost_usd == 1.0

    def test_a_page_of_only_processing_records_is_none(self):
        page = _page([_record("inv1", 0, 0, status="processing")])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            usage = for_run(base_url="https://gw", logs_key="k",
                            invocation_id="inv1", start_time=0, retry_seconds=0)
        assert usage is None


class TestDiagnosableFailures:
    """Absent billed cost reads as cost_source=estimate_only on the span no
    matter the reason. Each reason must name itself in the deployed logs, at
    WARNING -- the Agent Engine root logger swallows DEBUG and INFO.
    """

    def test_a_missing_key_says_which_credential(self, caplog):
        for_run(base_url="https://gw", logs_key="", invocation_id="inv1",
                start_time=0, retry_seconds=0)
        assert "BIFROST_LOGS_API_KEY is unset" in caplog.text
        assert caplog.records[0].levelname == "WARNING"

    def test_a_missing_base_url_says_which_variable(self, caplog):
        for_run(base_url="", logs_key="k", invocation_id="inv1",
                start_time=0, retry_seconds=0)
        assert "BIFROST_BASE_URL" in caplog.text
        assert caplog.records[0].levelname == "WARNING"

    def test_an_http_error_reports_the_status_code(self, caplog):
        error = urllib.error.HTTPError("https://gw/api/logs", 401,
                                       "Unauthorized", {}, None)
        with mock.patch.object(bifrost_usage, "_get_page", side_effect=error):
            for_run(base_url="https://gw", logs_key="k", invocation_id="inv1",
                    start_time=0, retry_seconds=0)
        assert "HTTP 401" in caplog.text

    def test_records_returned_but_none_matched_is_its_own_message(self, caplog):
        """The distinction that matters: the gateway answered, but our
        x-bf-lh-* attribution headers never reached it."""
        page = _page([_record("someone-elses-run", 1.0, 100)])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            for_run(base_url="https://gw", logs_key="k", invocation_id="inv1",
                    start_time=0, retry_seconds=0)
        assert "1 log records in the window, NONE matching" in caplog.text
        assert "x-bf-lh-" in caplog.text

    def test_an_unreachable_gateway_does_not_blame_the_headers(self, caplog):
        """It never answered, so it cannot report how many records it held."""
        with mock.patch.object(bifrost_usage, "_get_page",
                               side_effect=urllib.error.URLError("down")):
            for_run(base_url="https://gw", logs_key="k", invocation_id="inv1",
                    start_time=0, retry_seconds=0)
        assert "lookup failed" in caplog.text
        assert "NONE matching" not in caplog.text

    def test_the_success_path_reports_the_total(self, caplog):
        page = _page([_record("inv1", 1.5, 150, sub_agent="sentry_tierb")])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            for_run(base_url="https://gw", logs_key="k", invocation_id="inv1",
                    start_time=0, retry_seconds=0)
        assert "Bifrost billed this run: 1.50000 USD, 150 tokens, 1 records" in caplog.text

    def test_a_single_poll_budget_is_not_called_unsettled(self, caplog):
        """retry_seconds=0 asked for one poll; one poll is not a shortfall."""
        page = _page([_record("inv1", 1.5, 150)])
        with mock.patch.object(bifrost_usage, "_get_page", return_value=page):
            for_run(base_url="https://gw", logs_key="k", invocation_id="inv1",
                    start_time=0, retry_seconds=0)
        assert "UNSETTLED" not in caplog.text

    def test_a_deadline_cut_short_says_the_count_may_undercount(self, caplog):
        """The count was still growing when the budget ran out."""
        pages = [_page([_record("inv1", 1.0, 100)]),
                 _page([_record("inv1", 1.0, 100), _record("inv1", 2.0, 200)])]
        with mock.patch.object(bifrost_usage, "_get_page",
                               side_effect=pages * 4), \
                mock.patch.object(bifrost_usage.time, "sleep"), \
                mock.patch.object(bifrost_usage.time, "monotonic",
                                  side_effect=[0, 0, 999, 999]):
            for_run(base_url="https://gw", logs_key="k", invocation_id="inv1",
                    start_time=0, retry_seconds=30)
        assert "UNSETTLED at the deadline" in caplog.text

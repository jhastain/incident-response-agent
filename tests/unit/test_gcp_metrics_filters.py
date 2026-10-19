"""Regression tests for the gcp_metrics empty-result defect (2026-07-28 live runs).

The agent issued GCP Monitoring queries that matched no resource and then read the
zero-series answer as a healthy metric. Two distinct causes, both verified live against
acme-production-100000:

Cause 1: Cloud Run filters used short service names ("apiv2", "hangfire-recurring"). The
         real resource.labels.service_name values are region-suffixed deployed names such
         as "WebCore-prod-run-apiv2-us-c1", so the filter matched nothing.

Cause 2: Memorystore queries used the legacy redis.googleapis.com/* namespace. The prod
         instances are Memorystore for VALKEY, which publishes memorystore.googleapis.com/*;
         the redis.* metric types return no time series at all.

Cause 3 (reporting): an empty series was indistinguishable from a healthy one. Empty results
         now self-diagnose by naming the filter that matched nothing.
"""

import asyncio
from unittest import mock

from app.sub_agents.tier1 import gcp_metrics_tierb
from app.sub_agents.tier1.gcp_metrics import INSTRUCTION as TIER_A_INSTRUCTION
from app.sub_agents.tier1.gcp_metrics import RESOURCE_FILTER_GUIDANCE
from app.sub_agents.tier1.gcp_metrics_tierb import INSTRUCTION as TIER_B_INSTRUCTION
from app.sub_agents.tier1.gcp_metrics_tierb import _diagnose_empty, fetch_gcp_metrics

# Resource names confirmed live on 2026-07-28 via the GCP Monitoring API.
REAL_CLOUD_RUN_SERVICES = [
    "WebCore-prod-run-apiv2-us-c1",
    "WebCore-prod-run-hangfire-recurring-us-c1",
    "WebCore-prod-run-hangfire-background-us-c1",
]
REAL_VALKEY_INSTANCES = [
    "WebCore-prod-valkey-hangfire-us-c1",
    "WebCore-prod-valkey-sessionstate-us-c1",
    "WebCore-prod-valkey-dbbacked-us-c1",
    "WebCore-prod-valkey-persistent-us-c1",
]
# These returned zero series in the failing run.
DEAD_REDIS_METRICS = [
    "redis.googleapis.com/stats/memory/usage_ratio",
    "redis.googleapis.com/clients/connected",
    "redis.googleapis.com/clients/blocked",
    "redis.googleapis.com/commands/calls",
]


class TestValkeyMetricNamespace:
    def test_tier_b_no_longer_tells_the_agent_to_query_redis_metrics(self):
        for dead in DEAD_REDIS_METRICS:
            assert dead not in TIER_B_INSTRUCTION

    def test_tier_b_names_the_memorystore_metrics_that_return_data(self):
        assert (
            "memorystore.googleapis.com/instance/clients/total_connected_clients"
            in TIER_B_INSTRUCTION
        )
        assert (
            "memorystore.googleapis.com/instance/memory/average_utilization"
            in TIER_B_INSTRUCTION
        )
        assert (
            "memorystore.googleapis.com/instance/node/clients/blocked_clients"
            in TIER_B_INSTRUCTION
        )

    def test_tier_a_names_the_memorystore_namespace_and_not_redis(self):
        assert "memorystore.googleapis.com/instance/" in RESOURCE_FILTER_GUIDANCE
        for dead in DEAD_REDIS_METRICS:
            assert dead not in RESOURCE_FILTER_GUIDANCE

    def test_both_tiers_give_the_real_valkey_instance_ids(self):
        for instance_id in REAL_VALKEY_INSTANCES:
            assert instance_id in TIER_B_INSTRUCTION
            assert instance_id in RESOURCE_FILTER_GUIDANCE

    def test_both_tiers_name_instance_id_as_the_filter_label(self):
        assert "resource.labels.instance_id" in TIER_B_INSTRUCTION
        assert "resource.labels.instance_id" in RESOURCE_FILTER_GUIDANCE


class TestCloudRunServiceNameFilter:
    def test_both_tiers_give_the_full_region_suffixed_service_names(self):
        for service in REAL_CLOUD_RUN_SERVICES:
            assert service in TIER_B_INSTRUCTION
            assert service in RESOURCE_FILTER_GUIDANCE

    def test_both_tiers_use_the_singular_labels_form_of_the_filter_key(self):
        # resource.label.service_name (singular) is the wrong key and matches nothing.
        assert "resource.labels.service_name" in TIER_B_INSTRUCTION
        assert "resource.labels.service_name" in RESOURCE_FILTER_GUIDANCE

    def test_both_tiers_warn_that_short_service_names_match_nothing(self):
        assert "apiv2" in TIER_B_INSTRUCTION and "NOTHING" in TIER_B_INSTRUCTION
        assert "NOTHING" in RESOURCE_FILTER_GUIDANCE

    def test_tier_a_guidance_is_actually_wired_into_the_prompt(self):
        assert "WebCore-prod-run-apiv2-us-c1" in TIER_A_INSTRUCTION


class TestEmptyResultIsSelfDiagnosing:
    def test_empty_series_names_the_filter_that_matched_nothing(self):
        q = {
            "tool": "query_time_series",
            "project_id": "acme-production-100000",
            "metric_type": "run.googleapis.com/container/memory/utilizations",
            "filter_str": 'resource.labels.service_name="hangfire-recurring"',
            "interval_minutes": 120,
        }
        out = _diagnose_empty(q, "[]")

        assert "NO SERIES MATCHED" in out
        assert "run.googleapis.com/container/memory/utilizations" in out
        assert 'resource.labels.service_name="hangfire-recurring"' in out
        assert "does NOT mean the resource is healthy" in out

    def test_empty_series_with_no_filter_says_so(self):
        q = {
            "tool": "query_time_series",
            "project_id": "acme-production-100000",
            "metric_type": "redis.googleapis.com/clients/connected",
            "interval_minutes": 60,
        }
        out = _diagnose_empty(q, "[]")

        assert "NO SERIES MATCHED" in out
        assert "no filter (whole project)" in out

    def test_non_empty_series_is_passed_through_untouched(self):
        payload = '[{"metric": {}, "resource": {}, "points": [{"value": 0.19}]}]'
        q = {
            "tool": "query_time_series",
            "metric_type": "run.googleapis.com/container/memory/utilizations",
        }
        assert _diagnose_empty(q, payload) == payload

    def test_zero_valued_points_are_not_treated_as_empty(self):
        # A real series of zeros is evidence; only a zero-SERIES result is not.
        payload = '[{"metric": {}, "resource": {}, "points": [{"value": 0}]}]'
        q = {"tool": "query_time_series", "metric_type": "x"}
        assert _diagnose_empty(q, payload) == payload

    def test_empty_alert_policy_list_is_not_rewritten(self):
        q = {"tool": "list_alert_policies", "project_id": "acme-production-100000"}
        assert _diagnose_empty(q, "[]") == "[]"

    def test_fetch_gcp_metrics_diagnoses_an_empty_query_end_to_end(self):
        q = {
            "tool": "query_time_series",
            "project_id": "acme-production-100000",
            "metric_type": "redis.googleapis.com/clients/connected",
            "filter_str": 'resource.labels.instance_id="WebCore-valkey-prod"',
            "interval_minutes": 120,
        }
        with mock.patch.object(gcp_metrics_tierb, "query_time_series", return_value="[]"):
            with mock.patch.dict(
                gcp_metrics_tierb._DISPATCH,
                {"query_time_series": gcp_metrics_tierb.query_time_series},
            ):
                out = asyncio.run(fetch_gcp_metrics([q]))

        assert "NO SERIES MATCHED" in out
        assert 'resource.labels.instance_id="WebCore-valkey-prod"' in out

    def test_findings_rules_forbid_reading_an_empty_series_as_healthy(self):
        assert "NO SERIES MATCHED" in TIER_B_INSTRUCTION
        assert "REPORTABLE FINDING" in TIER_B_INSTRUCTION

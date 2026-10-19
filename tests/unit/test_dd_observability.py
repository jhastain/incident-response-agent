"""The Datadog enable path must be inert by default and unbreakable when on.

An observability library that raises takes down incident response with it. Every
test here is about the module refusing to become a failure mode: disabled by
default, no-op on missing config, swallow-and-log on a broken SDK.
"""

from __future__ import annotations

import inspect
import os
from unittest import mock

from app.app_utils import dd_observability


class TestDisabledByDefault:
    def test_no_env_means_no_op(self, monkeypatch):
        monkeypatch.delenv("DD_LLMOBS_ENABLED", raising=False)
        with mock.patch.object(dd_observability, "_enable_llmobs") as enable:
            assert dd_observability.setup_datadog_observability() is False
        enable.assert_not_called()

    def test_explicit_false_means_no_op(self, monkeypatch):
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "false")
        with mock.patch.object(dd_observability, "_enable_llmobs") as enable:
            assert dd_observability.setup_datadog_observability() is False
        enable.assert_not_called()

    def test_missing_api_key_does_not_enable(self, monkeypatch):
        """Enabling without a key would fail inside the SDK. Refuse earlier, loudly."""
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
        monkeypatch.delenv("DD_API_KEY", raising=False)
        with mock.patch.object(dd_observability, "_enable_llmobs") as enable:
            assert dd_observability.setup_datadog_observability() is False
        enable.assert_not_called()


class TestEnabledConfiguration:
    def test_passes_explicit_ml_app_and_defaults(self, monkeypatch):
        """ml_app must be explicit: it propagates, and Bifrost upstream sets
        ml_app=bifrost-gateway. Relying on the default risks IR spans being
        grouped under the gateway."""
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
        monkeypatch.setenv("DD_API_KEY", "k")
        for var in ("DD_LLMOBS_ML_APP", "DD_SITE", "DD_ENV", "DD_SERVICE",
                    "COMMIT_SHA", "DD_LLMOBS_SAMPLE_RATE", "DD_VERSION"):
            monkeypatch.delenv(var, raising=False)

        with mock.patch.object(dd_observability, "_enable_llmobs") as enable:
            assert dd_observability.setup_datadog_observability() is True

        kwargs = enable.call_args.kwargs
        # The internal- prefix is what puts the agent on the internal-* rollup.
        assert kwargs["ml_app"] == "ir-agent"
        assert kwargs["site"] == "us5.datadoghq.com"
        assert kwargs["agentless_enabled"] is True
        # NOT "dev". The per-environment Datadog dashboards and monitors
        # (infrastructure/<env>/global/datadog/*) filter on env:<env>, so
        # defaulting an unset DD_ENV to "dev" would file test and prod spans under dev
        # — polluting dev's dashboards while leaving the real environment's silent.
        assert kwargs["env"] == "unset"
        assert kwargs["service"] == "ir-agent"
        assert kwargs["sample_rate"] == 1.0

    def test_ml_app_env_override_is_honored(self, monkeypatch):
        """DD_LLMOBS_ML_APP wins over the ML_APP constant."""
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_LLMOBS_ML_APP", "ir-agent-scratch")

        with mock.patch.object(dd_observability, "_enable_llmobs") as enable:
            assert dd_observability.setup_datadog_observability() is True

        assert enable.call_args.kwargs["ml_app"] == "ir-agent-scratch"
        # DD_SERVICE is a DIFFERENT tag and must not follow the ml_app rename.
        assert enable.call_args.kwargs["service"] == "ir-agent"

    def test_resolved_ml_app_matches_span_tagging(self, monkeypatch):
        """submit_evaluation's ml_app must equal the span's or Datadog drops the
        evaluation as orphaned, so both must resolve identically."""
        monkeypatch.setenv("DD_LLMOBS_ML_APP", "ir-agent-scratch")
        assert dd_observability.resolved_ml_app() == "ir-agent-scratch"

        monkeypatch.delenv("DD_LLMOBS_ML_APP", raising=False)
        assert dd_observability.resolved_ml_app() == dd_observability.ML_APP

    def test_env_overrides_are_honored(self, monkeypatch):
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "1")
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_ENV", "prod")
        monkeypatch.setenv("COMMIT_SHA", "abc123")
        monkeypatch.setenv("DD_LLMOBS_SAMPLE_RATE", "0.5")
        monkeypatch.delenv("DD_VERSION", raising=False)

        with mock.patch.object(dd_observability, "_enable_llmobs") as enable:
            assert dd_observability.setup_datadog_observability() is True

        kwargs = enable.call_args.kwargs
        assert kwargs["env"] == "prod"
        assert "version" not in kwargs
        assert os.environ["DD_VERSION"] == "abc123"
        assert kwargs["sample_rate"] == 0.5

    def test_unparseable_sample_rate_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_LLMOBS_SAMPLE_RATE", "not-a-number")
        monkeypatch.delenv("DD_VERSION", raising=False)

        with mock.patch.object(dd_observability, "_enable_llmobs") as enable:
            assert dd_observability.setup_datadog_observability() is True

        assert enable.call_args.kwargs["sample_rate"] == 1.0


class TestRealSdkSignature:
    def test_enable_llmobs_kwargs_match_real_sdk_signature(self, monkeypatch):
        """Guard against SDK signature drift.

        The tests above mock `_enable_llmobs`, so they can't catch a kwarg that
        LLMObs.enable() doesn't actually accept — that's exactly how the
        `version=` kwarg (not a real parameter) shipped and silently disabled
        Datadog in every environment while the exception was swallowed. Call
        through to the real signature and assert every kwarg we pass is valid.
        """
        from ddtrace.llmobs import LLMObs

        accepted = set(inspect.signature(LLMObs.enable).parameters)

        monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
        monkeypatch.setenv("DD_API_KEY", "k")

        captured = {}

        def fake_enable(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(LLMObs, "enable", staticmethod(fake_enable))

        assert dd_observability.setup_datadog_observability() is True

        unknown = set(captured) - accepted
        assert not unknown, f"kwargs not accepted by LLMObs.enable(): {unknown}"


class TestNeverRaises:
    def test_sdk_explosion_is_swallowed(self, monkeypatch):
        """If ddtrace itself fails, the agent still runs. This is the whole point."""
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.delenv("DD_VERSION", raising=False)
        with mock.patch.object(
            dd_observability, "_enable_llmobs", side_effect=RuntimeError("boom")
        ):
            assert dd_observability.setup_datadog_observability() is False
        assert dd_observability.is_enabled() is False

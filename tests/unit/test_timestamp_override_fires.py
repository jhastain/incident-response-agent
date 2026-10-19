"""The epoch-param override must actually receive the alert text.

`_backfill_alert_timestamp` exists to force `alert_timestamp` to the epoch
parameter embedded in the alert, because the model demonstrably invents dates
(2026-07-28: model said 2025-08-19 while the alert carried 2026-07-21).

Observed live 2026-07-31: the alert carried `from_ts=1785443700000`
(= 2026-07-30T20:35:00Z), `extract_epoch_timestamp` returns that correctly in
isolation, and the pipeline still stored `2026-05-30T17:35:00Z` — two months and
three hours off.

Cause: the callback looked for the alert in state keys `alert_text` / `alert` /
`user_input` / `input`. Nothing in the runner path sets any of them, so
`alert_text` was always `""`, `extract_epoch_timestamp("")` always returned `""`,
and the override never fired on any run. The guard was structurally dead while
appearing present — the same failure shape as the dropped `raw_text`.

A wrong timestamp is the most damaging single field in the pipeline: every
downstream log and metric query anchors on it, so the whole report becomes
internally consistent about the wrong day.
"""

from __future__ import annotations

from types import SimpleNamespace

from google.genai import types

from app.sub_agents.classifier import _backfill_alert_timestamp

# from_ts=1785443700000 ms -> 2026-07-30T20:35:00Z
ALERT = (
    "[Datadog Monitor] WebCore line-item update error spike\n"
    "Monitor: https://us5.datadoghq.com/monitors/1064301"
    "?from_ts=1785443700000&to_ts=1785447300000\n"
    "29,877 error spans on POST /api/orderorderitems/update, HTTP 500"
)
TRUE_TS = "2026-07-30T20:35:00Z"


def _ctx(classification: dict, *, user_text: str | None = ALERT, state_extra=None):
    state = {"classification": classification}
    if state_extra:
        state.update(state_extra)
    content = (
        types.Content(role="user", parts=[types.Part(text=user_text)])
        if user_text is not None
        else None
    )
    return SimpleNamespace(state=state, user_content=content)


class TestOverrideFiresFromInvocationContent:
    def test_model_invented_date_is_overridden(self):
        """The exact live failure: model says May, the alert says July."""
        classification = {"alert_timestamp": "2026-05-30T17:35:00Z"}
        ctx = _ctx(classification)

        _backfill_alert_timestamp(ctx)

        assert ctx.state["classification"]["alert_timestamp"] == TRUE_TS

    def test_the_overridden_value_is_recorded_not_discarded(self):
        """Keep what the model said, so a bad-date pattern stays auditable."""
        classification = {"alert_timestamp": "2026-05-30T17:35:00Z"}
        ctx = _ctx(classification)

        _backfill_alert_timestamp(ctx)
        out = ctx.state["classification"]

        assert out["alert_timestamp_model_value"] == "2026-05-30T17:35:00Z"
        assert "epoch-param-override" in out["alert_timestamp_source"]

    def test_unknown_is_backfilled(self):
        classification = {"alert_timestamp": "unknown"}
        ctx = _ctx(classification)

        _backfill_alert_timestamp(ctx)
        out = ctx.state["classification"]

        assert out["alert_timestamp"] == TRUE_TS
        assert out["alert_timestamp_source"] == "epoch-param-backfill"

    def test_explicit_state_key_still_wins_when_present(self):
        """Callers that pre-seed the text keep working — user_content is a
        fallback, not a replacement."""
        classification = {"alert_timestamp": "unknown"}
        ctx = _ctx(
            classification,
            user_text="no epoch here",
            state_extra={"alert_raw_text": ALERT},
        )

        _backfill_alert_timestamp(ctx)

        assert ctx.state["classification"]["alert_timestamp"] == TRUE_TS


class TestGuardsPreserved:
    def test_no_epoch_anywhere_marks_model_value_unverified(self):
        """No epoch to check against: keep the model's value but say so, rather
        than letting a window built on it read as ground truth."""
        classification = {"alert_timestamp": "2026-07-30T12:00:00Z"}
        ctx = _ctx(classification, user_text="alert with no epoch parameter")

        _backfill_alert_timestamp(ctx)

        assert (
            ctx.state["classification"]["alert_timestamp_source"]
            == "model-asserted (no epoch param to verify)"
        )

    def test_nothing_at_all_is_marked_missing(self):
        classification = {"alert_timestamp": "unknown"}
        ctx = _ctx(classification, user_text="no time reference")

        _backfill_alert_timestamp(ctx)

        assert "MISSING" in ctx.state["classification"]["alert_timestamp_source"]

    def test_absent_user_content_does_not_raise(self):
        """Deployed callers may not populate user_content; degrade, never crash."""
        classification = {"alert_timestamp": "unknown"}
        ctx = _ctx(classification, user_text=None)

        _backfill_alert_timestamp(ctx)

        assert "MISSING" in ctx.state["classification"]["alert_timestamp_source"]

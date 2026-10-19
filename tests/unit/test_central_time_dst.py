"""Central time must follow DST — CST in winter, CDT in summer.

Observed 2026-07-31 in a live report: "2026-07-30T20:35:00Z UTC / 14:35:00 CST".
July is CDT (UTC-5), so 20:35Z is 15:35 CDT. The report was wrong by an hour AND
labelled with the wrong zone.

Cause: `formatting.py` defined `CST = timezone(timedelta(hours=-6))` — a fixed
offset that never observes daylight saving — and the report prompt instructed
"Convert all UTC timestamps to Central Standard Time (UTC-6)", so the model
reproduced the same error independently.

Central time is UTC-6 (CST) from early November to mid-March and UTC-5 (CDT) the
rest of the year. Roughly two thirds of the calendar is CDT, so the fixed offset
is wrong more often than it is right. Every incident report leads with a
timestamp; an hour of skew there sends a responder to the wrong window of logs.

The verification agent already flags this class of error
(`verification.py`: "CST is UTC-6; CDT is UTC-5 and applies March-November"),
which means the pipeline was catching its own bug and reporting it as a finding
rather than fixing it.
"""

from __future__ import annotations

from app.tools.formatting import utc_to_cst


class TestSummerIsCDT:
    def test_july_afternoon_converts_to_cdt(self):
        """The exact live failure: 20:35Z on 2026-07-30."""
        got = utc_to_cst("2026-07-30T20:35:00Z")
        assert "15:35" in got, got
        assert "CDT" in got, got
        assert "CST" not in got, got

    def test_july_midnight_rolls_back_a_day(self):
        got = utc_to_cst("2026-07-15T02:00:00Z")
        assert "2026-07-14" in got, got
        assert "21:00" in got, got
        assert "CDT" in got, got


class TestWinterIsCST:
    def test_january_converts_to_cst(self):
        got = utc_to_cst("2026-01-15T20:35:00Z")
        assert "14:35" in got, got
        assert "CST" in got, got
        assert "CDT" not in got, got


class TestTransitions:
    def test_day_before_spring_forward_is_cst(self):
        """2026 DST begins Sunday 2026-03-08. The 7th is still CST."""
        got = utc_to_cst("2026-03-07T18:00:00Z")
        assert "12:00" in got, got
        assert "CST" in got, got

    def test_day_after_spring_forward_is_cdt(self):
        got = utc_to_cst("2026-03-09T18:00:00Z")
        assert "13:00" in got, got
        assert "CDT" in got, got

    def test_day_after_fall_back_is_cst(self):
        """2026 DST ends Sunday 2026-11-01. The 2nd is CST again."""
        got = utc_to_cst("2026-11-02T18:00:00Z")
        assert "12:00" in got, got
        assert "CST" in got, got


class TestInputFormats:
    def test_epoch_seconds_supported(self):
        # 1785443700 = 2026-07-30T20:35:00Z
        got = utc_to_cst("1785443700")
        assert "15:35" in got, got
        assert "CDT" in got, got

    def test_offset_form_supported(self):
        got = utc_to_cst("2026-07-30T20:35:00+00:00")
        assert "15:35" in got, got

    def test_garbage_still_reports_invalid(self):
        assert "invalid timestamp" in utc_to_cst("not a timestamp")

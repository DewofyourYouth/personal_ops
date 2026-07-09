"""Tests for QuietWindow — Shabbat + chag quiet-window logic."""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from quiet_window import QuietWindow

_TZ_STR = "Asia/Jerusalem"


def _dt(iso: str) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(_TZ_STR))


def _make_shabbat(candle_time=None):
    shabbat = MagicMock()
    shabbat.load_candle_lighting.return_value = candle_time
    return shabbat


class TestShabbatQuiet:
    def test_saturday_morning_is_quiet(self):
        # 2026-06-20 is a Saturday; 09:00 is before nightfall (21:00)
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-20T09:00:00+03:00")
        assert qw.is_quiet_at(dt) is True

    def test_saturday_after_nightfall_not_quiet(self):
        # Saturday 21:30 is past nightfall (21:00)
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-20T21:30:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_saturday_at_exactly_nightfall_not_quiet(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-20T21:00:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_friday_before_candles_not_quiet(self):
        from datetime import time
        # Candles at 19:30; 20-min buffer → quiet from 19:10
        qw = QuietWindow(_make_shabbat(candle_time=time(19, 30)))
        dt = _dt("2026-06-19T18:00:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_friday_after_candle_buffer_is_quiet(self):
        from datetime import time
        qw = QuietWindow(_make_shabbat(candle_time=time(19, 30)))
        # 19:10 is exactly at the quiet-start (19:30 - 20 min)
        dt = _dt("2026-06-19T19:15:00+03:00")
        assert qw.is_quiet_at(dt) is True

    def test_friday_no_candles_set_not_quiet(self):
        # Without a candle time, Friday is not considered quiet
        qw = QuietWindow(_make_shabbat(candle_time=None))
        dt = _dt("2026-06-19T20:00:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_monday_is_never_quiet(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T14:00:00+03:00")
        assert qw.is_quiet_at(dt) is False


class TestChagQuietWindow:
    def _qw_with_chag(self, quiet_start: str, quiet_end: str) -> QuietWindow:
        chagim = [{"name": "Test Chag", "quiet_start": quiet_start, "quiet_end": quiet_end}]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(chagim, f)
            path = f.name
        return QuietWindow(_make_shabbat(), chagim_path=path)

    def test_inside_chag_window_is_quiet(self):
        qw = self._qw_with_chag(
            "2026-09-10T18:00:00+03:00", "2026-09-11T21:00:00+03:00"
        )
        dt = _dt("2026-09-10T20:00:00+03:00")
        assert qw.is_quiet_at(dt) is True

    def test_before_chag_window_not_quiet(self):
        qw = self._qw_with_chag(
            "2026-09-10T18:00:00+03:00", "2026-09-11T21:00:00+03:00"
        )
        dt = _dt("2026-09-10T17:00:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_after_chag_window_not_quiet(self):
        qw = self._qw_with_chag(
            "2026-09-10T18:00:00+03:00", "2026-09-11T21:00:00+03:00"
        )
        dt = _dt("2026-09-11T21:30:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_chag_end_boundary_not_quiet(self):
        # quiet_end is exclusive
        qw = self._qw_with_chag(
            "2026-09-10T18:00:00+03:00", "2026-09-11T21:00:00+03:00"
        )
        dt = _dt("2026-09-11T21:00:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_missing_chag_file_is_safe(self):
        qw = QuietWindow(_make_shabbat(), chagim_path="/nonexistent/chagim.json")
        dt = _dt("2026-06-22T14:00:00+03:00")
        assert qw.is_quiet_at(dt) is False

    def test_malformed_chag_file_is_safe(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("not json at all {{{")
            path = f.name
        qw = QuietWindow(_make_shabbat(), chagim_path=path)
        dt = _dt("2026-06-22T14:00:00+03:00")
        assert qw.is_quiet_at(dt) is False


class TestWakingHours:
    def test_waking_hours_midday(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T12:00:00+03:00")
        assert qw.in_waking_hours(dt) is True

    def test_before_waking_start(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T07:59:00+03:00")
        assert qw.in_waking_hours(dt) is False

    def test_waking_end_boundary(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T22:00:00+03:00")
        assert qw.in_waking_hours(dt) is True

    def test_past_waking_end(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T22:30:00+03:00")
        assert qw.in_waking_hours(dt) is False


class TestShouldPrompt:
    def test_weekday_midday_should_prompt(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T14:00:00+03:00")  # Monday
        assert qw.should_prompt(dt) is True

    def test_shabbat_should_not_prompt(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-20T10:00:00+03:00")  # Saturday morning
        assert qw.should_prompt(dt) is False

    def test_nighttime_should_not_prompt(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T23:00:00+03:00")  # Monday night
        assert qw.should_prompt(dt) is False


class TestBackwardCompat:
    def test_quiet_now_shim(self):
        qw = QuietWindow(_make_shabbat())
        # Just verify it's callable and returns a bool
        result = qw.quiet_now()
        assert isinstance(result, bool)

    def test_in_active_window_shim(self):
        qw = QuietWindow(_make_shabbat())
        result = qw.in_active_window()
        assert isinstance(result, bool)

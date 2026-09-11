"""Tests for QuietWindow — Shabbat + chag quiet-window logic."""

import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pyluach.dates import HebrewDate

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from quiet_window import QuietWindow

_TZ_STR = "Asia/Jerusalem"


def _dt(iso: str) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.fromisoformat(iso).astimezone(ZoneInfo(_TZ_STR))


def _make_shabbat(candle_time=None, nightfall_time=None):
    from datetime import time as _time
    from zoneinfo import ZoneInfo

    shabbat = MagicMock()
    shabbat.load_candle_lighting.return_value = candle_time
    nt = nightfall_time or _time(21, 0)  # matches the old hardcoded default
    shabbat.computed_nightfall.side_effect = lambda d: datetime.combine(
        d, nt, tzinfo=ZoneInfo(_TZ_STR)
    )
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

    def test_saturday_nightfall_uses_shabbat_computed_nightfall(self):
        from datetime import time

        # A later-than-default nightfall (e.g. long summer sunset + 72 min)
        # should keep Saturday quiet past the old hardcoded 21:00 cutoff.
        qw = QuietWindow(_make_shabbat(nightfall_time=time(21, 45)))
        dt = _dt("2026-06-20T21:30:00+03:00")
        assert qw.is_quiet_at(dt) is True


class TestChagQuietWindow:
    def _qw_with_chag(self, quiet_start: str, quiet_end: str) -> QuietWindow:
        chagim = [
            {"name": "Test Chag", "quiet_start": quiet_start, "quiet_end": quiet_end}
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json at all {{{")
            path = f.name
        qw = QuietWindow(_make_shabbat(), chagim_path=path)
        dt = _dt("2026-06-22T14:00:00+03:00")
        assert qw.is_quiet_at(dt) is False


class TestActiveWindowName:
    def test_chag_window_reports_chag_name_not_shabbat(self):
        # Regression: a chag quiet window (e.g. Rosh Hashana) was previously
        # indistinguishable from Shabbat in is_quiet_at(), so callers hardcoded
        # "Shabbat" in user-facing text even when the actual quiet window was
        # a chag on a weekday.
        chagim = [
            {
                "name": "Rosh Hashana 5787 day 1",
                "quiet_start": "2026-09-10T18:00:00+03:00",
                "quiet_end": "2026-09-11T21:00:00+03:00",
            }
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(chagim, f)
            path = f.name
        qw = QuietWindow(_make_shabbat(), chagim_path=path)
        # 2026-09-11 is a Friday, so without the chag this wouldn't be quiet yet.
        dt = _dt("2026-09-11T09:00:00+03:00")
        assert qw.active_window_name(dt) == "Rosh Hashana 5787 day 1"

    def test_shabbat_window_reports_shabbat(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-20T09:00:00+03:00")  # Saturday morning
        assert qw.active_window_name(dt) == "Shabbat"

    def test_no_window_reports_none(self):
        qw = QuietWindow(_make_shabbat())
        dt = _dt("2026-06-22T14:00:00+03:00")  # Monday
        assert qw.active_window_name(dt) is None


class TestRealChagimFileDates:
    """Cross-checks every ops/chagim.json entry against the real Hebrew
    calendar (pyluach), so a hand-typed date can't silently drift.

    Regression for a data bug (not a logic bug): every 5787 (2026) entry, and
    the two 5786 Rosh Hashana entries, were dated one full day too early.
    quiet_end is checked here because that's the entry's actual chag day:
    quiet_start is candle lighting the evening *before*, so it always lands
    one calendar day earlier than the chag itself.

    The Hebrew month/day of each chag is a fixed calendrical fact (unlike its
    Gregorian date, which shifts every year), so the expected date is
    recomputed from pyluach rather than hardcoded — this stays correct as
    new years are appended to chagim.json instead of needing a table update
    for each one.
    """

    _CHAGIM_PATH = Path(__file__).parent.parent / "ops" / "chagim.json"
    _HEBREW_YEAR_RE = re.compile(r"\b(57\d\d)\b")

    # Name template (Hebrew year replaced with "{y}") -> (Hebrew month, day).
    # Month numbering is the civil count used by pyluach: Nisan=1 ... Tishrei=7.
    _CHAG_MONTH_DAY = {
        "Rosh Hashana {y} day 1": (7, 1),
        "Rosh Hashana {y} day 2": (7, 2),
        "Yom Kippur {y}": (7, 10),
        "Sukkot {y} (first day, Israel)": (7, 15),
        "Shemini Atzeret / Simchat Torah {y} (Israel)": (7, 22),
        "Pesach {y} first day (Israel)": (1, 15),
        "Pesach {y} last day (Israel)": (1, 21),
        "Shavuot {y} (Israel)": (3, 6),
    }

    def _expected_end_date(self, name: str) -> str:
        m = self._HEBREW_YEAR_RE.search(name)
        assert m, f"no Hebrew year found in chag name: {name!r}"
        hebrew_year = int(m.group(1))
        template = name.replace(m.group(1), "{y}", 1)
        month, day = self._CHAG_MONTH_DAY[template]
        return HebrewDate(hebrew_year, month, day).to_pydate().isoformat()

    def test_every_entry_matches_its_hebrew_calendar_date(self):
        entries = json.loads(self._CHAGIM_PATH.read_text())
        assert entries, "chagim.json is empty"
        for entry in entries:
            expected = self._expected_end_date(entry["name"])
            actual = entry["quiet_end"][:10]
            assert actual == expected, (
                f"{entry['name']}: quiet_end date is {actual}, expected "
                f"{expected} per the Hebrew calendar"
            )


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

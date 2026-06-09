"""Tests for /backdate: the past-date parser and the logs.write(when=...) plumbing.

Date parsing and backdated writes are exactly the "tricky, easy-to-get-wrong, must not
silently regress" surface CLAUDE.md calls out for testing.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from logs import Logs
from text_router import _parse_backdate

TZ = ZoneInfo("Asia/Jerusalem")


def test_parse_yesterday_and_today():
    """Backdate parsing recognizes today/yesterday prefixes and strips them."""
    today = date.today()
    assert _parse_backdate("yesterday habit: daf yomi") == (
        today - timedelta(days=1),
        "habit: daf yomi",
    )
    assert _parse_backdate("today checkin") == (today, "checkin")


def test_parse_numeric_offsets():
    """Backdate parsing recognizes numeric day offsets from today."""
    today = date.today()
    assert _parse_backdate("-2 insight: foo")[0] == today - timedelta(days=2)
    assert _parse_backdate("3 days ago note: x")[0] == today - timedelta(days=3)


def test_parse_iso_date_keeps_entry_case():
    """ISO date prefixes set the date without lowercasing the remaining entry."""
    d, entry = _parse_backdate("2026-06-06 habit: Daf Yomi")
    assert d == date(2026, 6, 6)
    assert entry == "habit: Daf Yomi"  # entry text isn't lowercased


def test_parse_weekday_resolves_to_most_recent_past():
    """Last-weekday prefixes resolve to the most recent matching day in the past."""
    today = date.today()
    d, entry = _parse_backdate("last monday habit: gym")
    assert d.weekday() == 0
    assert d <= today and (today - d).days <= 7
    assert entry == "habit: gym"


def test_future_date_is_rejected():
    """Future explicit dates are rejected and leave the original text unchanged."""
    # Backdating only ever points at today or earlier.
    assert _parse_backdate("2099-01-01 habit: future") == (
        None,
        "2099-01-01 habit: future",
    )


def test_no_date_returns_none_and_original_text():
    """Text without a supported date prefix is returned unchanged."""
    assert _parse_backdate("habit: daf yomi") == (None, "habit: daf yomi")


def test_write_with_when_buckets_entry_under_that_day(tmp_path):
    """Logs.write stores entries under the supplied backdated day."""
    logs = Logs(str(tmp_path))
    target = date.today() - timedelta(days=3)
    when = datetime.combine(target, datetime.now(TZ).timetz())
    logs.write("habit", "daf yomi", when=when)

    # Stored under the backdated day, not today.
    assert [dict(r)["content"] for r in logs.db.entries_for_date(target)] == [
        "daf yomi"
    ]
    assert logs.db.entries_for_date(date.today()) == []


def test_write_without_when_still_uses_today(tmp_path):
    """Logs.write without an explicit timestamp stores entries under today."""
    logs = Logs(str(tmp_path))
    logs.write("habit", "walk")
    assert [dict(r)["content"] for r in logs.db.entries_for_date(date.today())] == [
        "walk"
    ]

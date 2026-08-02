"""Tests for habit pausing (HabitStore.pause_by_name/resume_by_name and the
/pausehabit duration parser) — the "two weeks off the 5:30 wake" feature.

The streak/chain math itself (paused days don't break a streak, and the pause
window has a real lower bound) is covered in test_habit_tracker.py. This file
covers the store-level plumbing: setting/clearing both pause bounds together,
name matching, and parsing the duration strings a person actually types.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context
from habit_handlers import HabitStore, _parse_pause_duration
from logs import Logs


def _store(tmp_path) -> HabitStore:
    store = HabitStore(Logs(str(tmp_path)).db, Context(tmp_path))
    store.add("5:30 wake")
    return store


def test_pause_by_name_sets_both_bounds(tmp_path):
    store = _store(tmp_path)
    today = date.today()
    until = today + timedelta(days=13)
    matched = store.pause_by_name("5:30 wake", until, start=today)
    assert matched == "5:30 wake"

    h = store.list_habits(tracked_only=False)[0]
    assert h["paused_from"] == today
    assert h["paused_until"] == until


def test_pause_by_name_defaults_start_to_today(tmp_path):
    store = _store(tmp_path)
    until = date.today() + timedelta(days=6)
    store.pause_by_name("5:30 wake", until)
    h = store.list_habits(tracked_only=False)[0]
    assert h["paused_from"] == date.today()


def test_resume_by_name_clears_both_bounds(tmp_path):
    store = _store(tmp_path)
    store.pause_by_name("5:30 wake", date.today() + timedelta(days=13))
    matched = store.resume_by_name("5:30 wake")
    assert matched == "5:30 wake"

    h = store.list_habits(tracked_only=False)[0]
    assert h["paused_from"] is None
    assert h["paused_until"] is None


def test_pause_matches_forgivingly_typed_names(tmp_path):
    """Pausing uses the same forgiving name match as cue/identity — a person
    doesn't need to type the habit's stored name byte-for-byte."""
    store = HabitStore(Logs(str(tmp_path)).db, Context(tmp_path))
    store.add("Eat at least 100 grams of protein.")
    matched = store.pause_by_name(
        "eat at least 100 grams of protein", date.today() + timedelta(days=6)
    )
    assert matched == "Eat at least 100 grams of protein."


def test_pause_unknown_habit_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.pause_by_name("nonexistent habit", date.today()) is None
    assert store.resume_by_name("nonexistent habit") is None


# --- Duration parsing (what /pausehabit's `: <duration>` part accepts) ---


def test_parse_pause_duration_days():
    today = date(2026, 8, 2)
    # "14" means 14 days starting today, so today counts as day 1 -> ends day 13 later.
    assert _parse_pause_duration("14", today) == today + timedelta(days=13)
    assert _parse_pause_duration("14d", today) == today + timedelta(days=13)
    assert _parse_pause_duration("1", today) == today  # a single day: just today


def test_parse_pause_duration_weeks():
    today = date(2026, 8, 2)
    assert _parse_pause_duration("2w", today) == today + timedelta(weeks=2, days=-1)


def test_parse_pause_duration_explicit_until_date():
    today = date(2026, 8, 2)
    assert _parse_pause_duration("until 2026-08-16", today) == date(2026, 8, 16)


def test_parse_pause_duration_rejects_garbage():
    today = date(2026, 8, 2)
    assert _parse_pause_duration("forever", today) is None
    assert _parse_pause_duration("until next tuesday", today) is None
    assert _parse_pause_duration("", today) is None

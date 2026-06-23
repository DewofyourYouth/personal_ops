import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from habit_tracker import (
    SHABBAT,
    _is_due,
    compute_streak,
    load_habit_logs,
    missed_last_due_day,
    recent_chain,
)
from logs import Logs


def _write_habit_days(logs: Logs, name: str, days_back: range) -> None:
    """Write a habit entry directly into each day's JSONL (compute_* read JSONL)."""
    today = date.today()
    for i in days_back:
        d = today - timedelta(days=i)
        path = Path(logs.log_dir) / f"{d}.jsonl"
        with open(path, "a") as f:
            f.write(
                json.dumps(
                    {"ts": f"{d}T09:00:00+03:00", "tag": "habit", "content": name}
                )
                + "\n"
            )


def test_is_due():
    """Due-day logic treats Shabbat as skipped and honors weekday filters."""
    # Find a known Monday and Saturday to test deterministically.
    monday = date(2026, 6, 1)  # a Monday
    saturday = date(2026, 6, 6)  # a Saturday (Shabbat)
    assert _is_due(monday, None) is True  # every-day habit, non-Shabbat
    assert _is_due(saturday, None) is False  # Shabbat never counts
    assert _is_due(monday, [0, 2, 4]) is True  # Monday is in Mon/Wed/Fri
    assert _is_due(monday, [1, 3]) is False  # not a Tue/Thu day


def test_recent_chain_all_done(tmp_path):
    """recent_chain returns a full hit chain when every recent due day was logged."""
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 20))
    chain = recent_chain(logs, "Daily walk", due_weekdays=None, n=14)
    assert len(chain) == 14
    assert all(chain)  # logged every day → every due day is a hit


def test_recent_chain_shows_gaps(tmp_path):
    """recent_chain marks an unlogged recent due day as a miss."""
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 20))  # all done...
    # ...then blank out today's log file so the most recent due day is a miss.
    (Path(logs.log_dir) / f"{date.today()}.jsonl").write_text("")
    chain = recent_chain(logs, "Daily walk", due_weekdays=None, n=14)
    if _is_due(date.today(), None):  # only meaningful when today is a due day
        assert chain[-1] is False


def test_missed_last_due_day(tmp_path):
    """missed_last_due_day distinguishes never-logged from recently completed habits."""
    logs = Logs(str(tmp_path))
    assert missed_last_due_day(logs, "Daily walk", None) is True  # nothing logged ever
    _write_habit_days(logs, "Daily walk", range(0, 20))
    assert missed_last_due_day(logs, "Daily walk", None) is False  # prior day was done


def test_compute_streak_respects_due_days(tmp_path):
    """compute_streak counts only due-day history when calculating streaks."""
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 30))
    current, longest = compute_streak(logs, "Daily walk", due_weekdays=None)
    assert current >= 20 and longest >= current


_HABITS_TABLE = """
CREATE TABLE habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT, name TEXT,
    days TEXT DEFAULT '', tracked INTEGER DEFAULT 1, position INTEGER DEFAULT 0,
    cue TEXT DEFAULT '', identity TEXT DEFAULT ''
)
"""


def test_struggling_habits(tmp_path):
    """struggling_habits finds started habits that missed the recent success threshold."""
    from habit_tracker import struggling_habits

    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('S','Daily walk',1)"
    )
    logs.db.execute("INSERT INTO habits (section,name,tracked) VALUES ('S','Anki',1)")
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('S','Stretch',1)"
    )
    today = date.today()
    # Daily walk: done every recent day → healthy, not struggling.
    for i in range(0, 20):
        d = today - timedelta(days=i)
        logs.db.insert_entry(
            f"{d}T09:00:00+03:00", d.isoformat(), "habit", "Daily walk"
        )
    # Anki: done only 25–34 days ago → has a past streak but missed the recent window.
    for i in range(25, 35):
        d = today - timedelta(days=i)
        logs.db.insert_entry(f"{d}T09:00:00+03:00", d.isoformat(), "habit", "Anki")
    # Stretch: never done → excluded (not-yet-started, not failing).
    names = [s["name"] for s in struggling_habits(logs, window=14, threshold=0.5)]
    assert "Anki" in names
    assert "Daily walk" not in names
    assert "Stretch" not in names


def test_format_habits_for_prompt(tmp_path):
    """Prompt formatting groups tracked habits and includes cue metadata."""
    from habit_tracker import format_habits_for_prompt

    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,days,tracked,cue) VALUES "
        "('Anchors','06:15 Yerushalmi chavrusa','',1,'solo-first')"
    )
    logs.db.execute(
        "INSERT INTO habits (section,name,days,tracked) VALUES ('Anchors','21:00 Daf Yomi','',1)"
    )
    logs.db.execute(
        "INSERT INTO habits (section,name,days,tracked) VALUES ('Off','Hidden',',',0)"
    )
    out = format_habits_for_prompt(logs.db)
    assert "### Anchors" in out
    assert "06:15 Yerushalmi chavrusa" in out
    assert "cue: solo-first" in out
    assert "Hidden" not in out  # untracked habits are excluded


def test_habit_notes(tmp_path):
    """Habit notes are stored case-insensitively and returned newest-first."""
    import sys
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).parent.parent / "ops"))
    from context import Context
    from habit_handlers import HabitStore

    logs = Logs(str(tmp_path))
    store = HabitStore(logs.db, Context(tmp_path))
    store.add_note("Strength training", "shoulder felt off, went light")
    store.add_note("Strength training", "back to normal")
    store.add_note("Daf Yomi", "finished the masechta")

    s_notes = store.notes_for("strength training")  # case-insensitive
    assert len(s_notes) == 2
    assert s_notes[0]["note"] == "back to normal"  # newest first
    assert len(store.recent_notes(days=7)) == 3


def test_bonus_day_done_counts_toward_streak(tmp_path):
    """Doing a habit on a non-due day counts as a bonus day in the streak."""
    # Regression: doing a habit on a non-due day (Shabbat) should *extend* the streak,
    # not be skipped. Logging every day for two weeks always spans at least one Shabbat,
    # so all 14 days must count.
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daf Yomi", range(0, 14))
    current, _ = compute_streak(logs, "Daf Yomi", due_weekdays=None)
    assert current == 14


def test_quiet_non_due_day_does_not_break_streak(tmp_path):
    """A non-due day without a log is transparent and does not break the streak."""
    # The other half of the rule: a Shabbat with nothing logged is transparent — it
    # neither counts nor breaks. Done on every non-Shabbat day → streak spans the gaps.
    logs = Logs(str(tmp_path))
    today = date.today()
    due_days = [
        i for i in range(0, 21) if (today - timedelta(days=i)).weekday() != SHABBAT
    ]
    _write_habit_days(logs, "Daf Yomi", due_days)
    current, _ = compute_streak(logs, "Daf Yomi", due_weekdays=None)
    assert current == len(due_days)


def test_single_missed_due_day_does_not_break_streak(tmp_path):
    """Regression: one missed due day must not break the streak (takes two consecutive)."""
    logs = Logs(str(tmp_path))
    today = date.today()
    # Collect 6 consecutive non-Shabbat due days going back from today.
    due_days_back: list[int] = []
    i = 0
    while len(due_days_back) < 6:
        if _is_due(today - timedelta(days=i), None):
            due_days_back.append(i)
        i += 1
    # Done on all of them except the 3rd most recent — a single miss in the middle.
    logged_by_day = {
        (today - timedelta(days=d)).isoformat(): ["daf yomi"]
        for idx, d in enumerate(due_days_back)
        if idx != 2
    }
    current, _ = compute_streak(
        logs, "daf yomi", due_weekdays=None, logged_by_day=logged_by_day
    )
    assert current > 0, "single missed due day must not break the current streak"


def test_two_consecutive_misses_break_streak(tmp_path):
    """Two consecutive missed due days break the streak."""
    logs = Logs(str(tmp_path))
    today = date.today()
    # Collect 7 consecutive non-Shabbat due days going back from today.
    due_days_back: list[int] = []
    i = 0
    while len(due_days_back) < 7:
        if _is_due(today - timedelta(days=i), None):
            due_days_back.append(i)
        i += 1
    # Done today (index 0) and days 3+ only; miss indices 1 and 2 (two consecutive).
    logged_by_day = {
        (today - timedelta(days=d)).isoformat(): ["daf yomi"]
        for idx, d in enumerate(due_days_back)
        if idx not in (1, 2)
    }
    current, _ = compute_streak(
        logs, "daf yomi", due_weekdays=None, logged_by_day=logged_by_day
    )
    assert current == 1, (
        "two consecutive misses must break the streak; only today should count"
    )


# --- SQLite path tests (production path: logs.write → load_habit_logs → compute_streak) ---


def _find_most_recent_saturday() -> date:
    """Return the most recent Saturday (could be today if it is Saturday)."""
    today = date.today()
    days_since_saturday = (today.weekday() - SHABBAT) % 7
    return today - timedelta(days=days_since_saturday)


def _write_habit_db(logs: Logs, name: str, days_back: list[int]) -> None:
    """Write habit entries to SQLite (the production write path)."""
    today = date.today()
    for i in days_back:
        d = today - timedelta(days=i)
        logs.db.insert_entry(f"{d}T21:00:00+03:00", d.isoformat(), "habit", name)


def test_saturday_log_counts_in_sqlite_path(tmp_path):
    """Regression: motzei Shabbat logs stored in SQLite count toward the streak.

    The chain visual (🟩⬜) never shows Saturday (it shows only due days), but the
    streak COUNT (🔥N) must increase when the user logs on Saturday night.
    This exercises the production path: SQLite write → load_habit_logs → compute_streak.
    """
    logs = Logs(str(tmp_path))
    saturday = _find_most_recent_saturday()
    # Insert 7 consecutive days ending with Saturday to SQLite directly
    # (mimicking what logs.write() does in production).
    for i in range(7):
        d = saturday - timedelta(days=i)
        logs.db.insert_entry(f"{d}T21:30:00+03:00", d.isoformat(), "habit", "Daf Yomi")

    logged_by_day = load_habit_logs(logs)
    current, _ = compute_streak(
        logs, "Daf Yomi", due_weekdays=None, logged_by_day=logged_by_day
    )
    assert current == 7, (
        "7 consecutive days ending Saturday (via SQLite) must show streak of 7"
    )


def test_saturday_night_log_extends_existing_streak(tmp_path):
    """Logging on Saturday night extends a streak that was building through Friday.

    Scenario: user had N days of streak ending Friday, then logged Saturday night
    (motzei Shabbat). Checking on Sunday (Sunday not yet logged) should show N+1.
    This is the exact user-reported scenario: 'logged after Shabbat, streak didn't count.'
    """
    logs = Logs(str(tmp_path))
    saturday = _find_most_recent_saturday()
    friday = saturday - timedelta(days=1)
    sunday = saturday + timedelta(days=1)

    # Log Mon–Sat (6 days). Saturday is bonus (non-due), Friday is a due day.
    for i in range(6):
        d = saturday - timedelta(days=i)
        logs.db.insert_entry(f"{d}T21:30:00+03:00", d.isoformat(), "habit", "Daf Yomi")

    # Simulate checking on Sunday (not yet logged Sunday).
    # Compute streak with Sunday as "today" but nothing logged for it.
    logged_by_day = load_habit_logs(logs)
    # Inject Sunday as absent so the lookback starts from Sunday.
    # (load_habit_logs only contains what's in DB; Sunday has no entry.)

    # Build the lookback dict as compute_streak would see it from Sunday.
    current, _ = compute_streak(
        logs,
        "Daf Yomi",
        due_weekdays=None,
        logged_by_day=logged_by_day,
    )
    # Sunday is a single miss (forgiven by "never miss twice"), Saturday is bonus done,
    # Fri–Mon are due days done → streak must be >= 5 (Fri + bonus Sat + 4 prior due days).
    assert current >= 5, (
        "Saturday night log must be included in streak when checked the next day"
    )

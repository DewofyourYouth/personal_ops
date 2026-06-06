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
    # Find a known Monday and Saturday to test deterministically.
    monday = date(2026, 6, 1)  # a Monday
    saturday = date(2026, 6, 6)  # a Saturday (Shabbat)
    assert _is_due(monday, None) is True  # every-day habit, non-Shabbat
    assert _is_due(saturday, None) is False  # Shabbat never counts
    assert _is_due(monday, [0, 2, 4]) is True  # Monday is in Mon/Wed/Fri
    assert _is_due(monday, [1, 3]) is False  # not a Tue/Thu day


def test_recent_chain_all_done(tmp_path):
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 20))
    chain = recent_chain(logs, "Daily walk", due_weekdays=None, n=14)
    assert len(chain) == 14
    assert all(chain)  # logged every day → every due day is a hit


def test_recent_chain_shows_gaps(tmp_path):
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 20))  # all done...
    # ...then blank out today's log file so the most recent due day is a miss.
    (Path(logs.log_dir) / f"{date.today()}.jsonl").write_text("")
    chain = recent_chain(logs, "Daily walk", due_weekdays=None, n=14)
    if _is_due(date.today(), None):  # only meaningful when today is a due day
        assert chain[-1] is False


def test_missed_last_due_day(tmp_path):
    logs = Logs(str(tmp_path))
    assert missed_last_due_day(logs, "Daily walk", None) is True  # nothing logged ever
    _write_habit_days(logs, "Daily walk", range(0, 20))
    assert missed_last_due_day(logs, "Daily walk", None) is False  # prior day was done


def test_compute_streak_respects_due_days(tmp_path):
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

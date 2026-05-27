import sys
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from reminders import Reminders

TZ = ZoneInfo("Asia/Jerusalem")


@pytest.fixture
def rem(tmp_path):
    return Reminders(file_path=tmp_path / "reminders.json")


def test_load_empty(rem):
    assert rem.load() == []


def test_add_and_load(rem):
    entry = rem.add("Drink water", "daily", time="09:00")
    assert entry["text"] == "Drink water"
    assert entry["type"] == "daily"
    loaded = rem.load()
    assert len(loaded) == 1
    assert loaded[0]["id"] == entry["id"]


def test_remove(rem):
    e = rem.add("Take meds", "once", date="2026-05-27", time="08:00")
    rem.remove(e["id"])
    assert rem.load() == []


def test_remove_nonexistent_is_noop(rem):
    rem.add("Stay", "daily", time="10:00")
    rem.remove(str(uuid.uuid4()))
    assert len(rem.load()) == 1


def _at(hour: int, minute: int) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)


def test_due_now_daily_fires(rem):
    now = _at(9, 0)
    rem.add("Morning standup", "daily", time="09:00")
    with patch("reminders.datetime") as mock_dt:
        mock_dt.now.return_value = now
        due = rem.due_now()
    assert len(due) == 1
    assert due[0]["text"] == "Morning standup"


def test_due_now_daily_does_not_remove(rem):
    rem.add("Daily reminder", "daily", time="09:00")
    now = _at(9, 0)
    with patch("reminders.datetime") as mock_dt:
        mock_dt.now.return_value = now
        rem.due_now()
    assert len(rem.load()) == 1


def test_due_now_once_removes_after_firing(rem):
    import datetime as dt_mod
    today = dt_mod.date.today().isoformat()
    rem.add("One-time", "once", date=today, time="10:00")
    now = _at(10, 0)
    with patch("reminders.datetime") as mock_dt:
        mock_dt.now.return_value = now
        due = rem.due_now()
    assert len(due) == 1
    assert rem.load() == []


def test_due_now_interval_fires_at_boundary(rem):
    rem.add("Stretch", "interval", interval_minutes=30, window_start="08:00", window_end="22:00")
    now = _at(9, 0)
    with patch("reminders.datetime") as mock_dt:
        mock_dt.now.return_value = now
        due = rem.due_now()
    assert len(due) == 1


def test_due_now_interval_silent_outside_window(rem):
    rem.add("Stretch", "interval", interval_minutes=30, window_start="08:00", window_end="22:00")
    now = _at(3, 0)
    with patch("reminders.datetime") as mock_dt:
        mock_dt.now.return_value = now
        due = rem.due_now()
    assert len(due) == 0


def test_due_now_interval_not_off_boundary(rem):
    rem.add("Stretch", "interval", interval_minutes=30, window_start="08:00", window_end="22:00")
    now = _at(8, 17)
    with patch("reminders.datetime") as mock_dt:
        mock_dt.now.return_value = now
        due = rem.due_now()
    assert len(due) == 0


def test_due_now_daily_wrong_time(rem):
    rem.add("Lunch", "daily", time="12:00")
    now = _at(11, 0)
    with patch("reminders.datetime") as mock_dt:
        mock_dt.now.return_value = now
        due = rem.due_now()
    assert len(due) == 0

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from db import Database
from weight import WEGOVY_START_WEIGHT, Weight


@pytest.fixture
def weight(tmp_path):
    db = Database(str(tmp_path / "ops.db"))
    # Three weeks of one-per-day readings around the start weight.
    readings = [
        ("2025-11-12", 103.0),
        ("2025-11-14", 102.0),  # W46
        ("2025-11-18", 101.0),
        ("2025-11-20", 100.0),  # W47
        ("2025-11-25", 99.0),  # W48
    ]
    for d, kg in readings:
        db.insert_metric(f"{d}T07:00:00+02:00", d, "weight", str(kg), "kg")
    return Weight(db)


def test_total_lost(weight):
    total = weight.total_lost()
    assert total["current_kg"] == 99.0
    assert total["lost_kg"] == round(WEGOVY_START_WEIGHT - 99.0, 1)  # 4.5
    assert total["lost_lb"] == round((WEGOVY_START_WEIGHT - 99.0) * 2.20462, 1)


def test_latest_is_newest_first_with_deltas(weight):
    latest = weight.latest(3)
    assert [r["date"] for r in latest] == ["2025-11-25", "2025-11-20", "2025-11-18"]
    assert latest[0]["kg_lost"] == 4.5
    assert latest[0]["delta_since_start"] == -4.5


def test_weekly_averages_and_change(weight):
    weeks = weight.weekly_averages()
    # Newest week first.
    assert weeks[0]["week"] == "2025-W48"
    assert weeks[0]["avg"] == 99.0
    # W47 = mean(101, 100) = 100.5; W48 vs W47 = -1.5
    assert weeks[0]["delta_vs_prev"] == -1.5
    # Oldest week has no previous to compare against.
    assert weeks[-1]["delta_vs_prev"] is None


def test_latest_reading_per_day_wins(tmp_path):
    db = Database(str(tmp_path / "ops.db"))
    db.insert_metric("2025-11-20T07:00:00+02:00", "2025-11-20", "weight", "100.0", "kg")
    db.insert_metric("2025-11-20T20:00:00+02:00", "2025-11-20", "weight", "99.5", "kg")
    w = Weight(db)
    assert w.total_lost()["current_kg"] == 99.5  # the later same-day reading

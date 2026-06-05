import sys
from datetime import date, timedelta
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


def test_summary_pct_and_rate(tmp_path):
    # Steady loss over ~4 weeks; check % of bodyweight and the kg/week slope sign.
    db = Database(str(tmp_path / "ops.db"))
    start = date.today() - timedelta(days=28)
    for i in range(29):  # 100.0 down to 96.0 linearly over 28 days
        d = (start + timedelta(days=i)).isoformat()
        kg = round(100.0 - i * (4.0 / 28), 2)
        db.insert_metric(f"{d}T07:00:00+02:00", d, "weight", str(kg), "kg")
    s = Weight(db).summary()
    assert s["rate_kg_per_week"] is not None and s["rate_kg_per_week"] < 0  # losing
    assert abs(s["rate_kg_per_week"] - (-1.0)) < 0.1  # ~4kg/4wk = ~1kg/wk
    assert s["pct_of_bodyweight"] > 0
    # Endpoints are smoothed 7-day averages, not the single first/last reading.
    assert s["current_7day_avg_kg"] < s["start_week_avg_kg"]


def test_summary_none_when_empty(tmp_path):
    assert Weight(Database(str(tmp_path / "ops.db"))).summary() is None

"""Tests for the affect-proxy join in ops/mine_logs.py.

The nearest-timestamp matching and gap cutoff are the parts easy to get subtly
wrong (picking the wrong tap, or letting a far-apart tap masquerade as ground
truth for a note) — everything else in the module is descriptive reporting,
easy to eyeball from the printed output.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

from logs import Logs
from mine_logs import AFFECT_MAX_GAP_MIN, load_affect_pairs

TZ = ZoneInfo("Asia/Jerusalem")


def _dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 10, hour, minute, tzinfo=TZ)


def _mood_metric(value: int) -> dict:
    return {"key": "self_mood_rating", "value": value, "unit": ""}


def test_matches_voice_note_to_nearest_mood_tap_within_gap(tmp_path):
    logs = Logs(str(tmp_path))
    logs.write(
        "checkin",
        "voice note",
        extra={"affect_features": {"pitch_var": 12.0, "pause_count": 2}},
        when=_dt(10, 0),
    )
    # nearest tap (5 min later) should win over a further one
    logs.write("metric", "self_mood_rating 4", extra=_mood_metric(4), when=_dt(10, 5))
    logs.write("metric", "self_mood_rating 1", extra=_mood_metric(1), when=_dt(11, 0))

    pairs = load_affect_pairs(sqlite3.connect(logs.db.path))

    assert len(pairs) == 1
    assert pairs[0]["mood"] == 4
    assert pairs[0]["pitch_var"] == 12.0


def test_drops_pairs_wider_than_max_gap(tmp_path):
    logs = Logs(str(tmp_path))
    logs.write(
        "checkin",
        "voice note",
        extra={"affect_features": {"pitch_var": 5.0}},
        when=_dt(9, 0),
    )
    # only tap available is well outside the gap window -> no pair
    far_minute = int(AFFECT_MAX_GAP_MIN) + 15
    logs.write(
        "metric",
        "self_mood_rating 3",
        extra=_mood_metric(3),
        when=_dt(9, far_minute),
    )

    pairs = load_affect_pairs(sqlite3.connect(logs.db.path))

    assert pairs == []


def test_entries_without_affect_features_are_ignored(tmp_path):
    logs = Logs(str(tmp_path))
    logs.write("note", "just a text note", when=_dt(10, 0))
    logs.write("metric", "self_mood_rating 3", extra=_mood_metric(3), when=_dt(10, 1))

    pairs = load_affect_pairs(sqlite3.connect(logs.db.path))

    assert pairs == []

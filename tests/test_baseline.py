import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from baseline_tracker import Baseline
from logs import Logs


@pytest.fixture
def logs(tmp_path):
    return Logs(str(tmp_path))


def test_mood_energy_for_range_collects_numeric(logs):
    logs.write_metric("mood", 4)
    logs.write_metric("energy", 2)
    logs.write_metric("mood", 2)
    today = date.today()
    moods, energies = logs.mood_energy_for_range(today, today)
    assert sorted(moods) == [2, 4]
    assert energies == [2]


def test_mood_energy_normalizes_legacy_labels(logs):
    # Old data stored labels/emoji rather than 1-5 / 1-3 integers.
    logs.write_metric("mood", "great")  # -> 5
    logs.write_metric("energy", "drained")  # -> 1
    today = date.today()
    moods, energies = logs.mood_energy_for_range(today, today)
    assert moods == [5]
    assert energies == [1]


def test_mood_energy_falls_back_to_jsonl(tmp_path):
    # A day with no DB rows (pre-migration) must still be read from JSONL.
    logs = Logs(str(tmp_path))
    past = date.today() - timedelta(days=3)
    jsonl = tmp_path / f"{past}.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "ts": f"{past}T10:00:00+03:00",
                "tag": "metric",
                "key": "mood",
                "value": 3,
                "content": "mood 3",
            }
        )
        + "\n"
        + json.dumps(
            {
                "ts": f"{past}T10:01:00+03:00",
                "tag": "metric",
                "key": "energy",
                "value": 1,
                "content": "energy 1",
            }
        )
        + "\n"
    )
    moods, energies = logs.mood_energy_for_range(past, past)
    assert moods == [3]
    assert energies == [1]


def test_compute_week_includes_mood_energy(logs):
    logs.write_metric("mood", 4)
    logs.write_metric("mood", 2)
    logs.write_metric("energy", 3)
    baseline = Baseline(logs.log_dir)
    entry = baseline._compute_week(logs)
    assert entry["mood_avg"] == 3.0  # (4 + 2) / 2
    assert entry["energy_avg"] == 3.0


def test_compute_week_no_metrics_is_none(logs):
    # No mood/energy logged -> averages are None, not 0 (0 would imply a real low reading).
    entry = Baseline(logs.log_dir)._compute_week(logs)
    assert entry["mood_avg"] is None
    assert entry["energy_avg"] is None


def test_format_for_prompt_renders_mood_energy(logs):
    logs.write_metric("mood", 5)
    logs.write_metric("energy", 2)
    baseline = Baseline(logs.log_dir)
    baseline.compute_and_save_weekly(logs)
    out = baseline.format_for_prompt()
    assert "Mood" in out and "Energy" in out
    assert "5.0" in out  # the rendered weekly mood average

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from logs import Logs


@pytest.fixture
def log_dir(tmp_path):
    return Logs(str(tmp_path))


def test_write_and_read_today(log_dir):
    log_dir.write("note", "hello world")
    entries = log_dir.read_today()
    assert len(entries) == 1
    assert entries[0]["tag"] == "note"
    assert entries[0]["content"] == "hello world"


def test_write_metric(log_dir):
    log_dir.write_metric("weight", 75.5, "kg")
    entries = log_dir.read_today()
    assert entries[0]["tag"] == "metric"
    assert entries[0]["key"] == "weight"
    assert entries[0]["value"] == 75.5
    assert entries[0]["unit"] == "kg"


def test_read_recent_skips_metrics(tmp_path):
    logs = Logs(str(tmp_path))
    yesterday = date.today() - timedelta(days=1)
    jsonl = tmp_path / f"{yesterday}.jsonl"
    jsonl.write_text(
        json.dumps({"ts": "2026-05-26T10:00:00+03:00", "tag": "note", "content": "readable entry"}) + "\n"
        + json.dumps({"ts": "2026-05-26T10:01:00+03:00", "tag": "metric", "key": "weight", "value": 75, "unit": "kg", "content": "weight 75kg"}) + "\n"
    )
    recent = logs.read_recent(days=1)
    assert "readable entry" in recent
    assert "metric" not in recent
    assert "weight" not in recent


def test_load_metrics(tmp_path):
    logs = Logs(str(tmp_path))
    yesterday = date.today() - timedelta(days=1)
    jsonl = tmp_path / f"{yesterday}.jsonl"
    jsonl.write_text(
        json.dumps({"ts": "2026-05-26T10:00:00+03:00", "tag": "metric", "key": "steps", "value": 8000, "unit": "", "content": "steps 8000"}) + "\n"
    )
    metrics = logs.load_metrics(days=2)
    assert "steps" in metrics
    assert metrics["steps"][0][1] == 8000


def test_parse_md_fallback(tmp_path):
    logs = Logs(str(tmp_path))
    yesterday = date.today() - timedelta(days=1)
    md = tmp_path / f"{yesterday}.md"
    md.write_text("## 09:00 #insight\nSomething interesting happened\n")
    recent = logs.read_recent(days=1)
    assert "Something interesting happened" in recent


def test_read_recent_no_logs(log_dir):
    result = log_dir.read_recent(days=3)
    assert result == "No recent logs."


def test_read_recent_includes_today(log_dir):
    log_dir.write("note", "today's entry")
    result = log_dir.read_recent(days=1)
    assert "today's entry" in result


def test_compute_stats_completion(tmp_path):
    logs = Logs(str(tmp_path))
    today = date.today()
    agenda = {"items": [
        {"id": 0, "text": "Do something", "status": "done", "source": "llm"},
        {"id": 1, "text": "Do another", "status": "missed", "source": "llm"},
        {"id": 2, "text": "Open item", "status": "open", "source": "llm"},
    ]}
    (tmp_path / f"{today}-agenda.json").write_text(json.dumps(agenda))
    stats = logs.compute_stats(days=1)
    s = stats[str(today)]
    assert s["completion"] == (1, 2)  # open items excluded


def test_compute_stats_anchors(tmp_path):
    logs = Logs(str(tmp_path))
    today = date.today()
    agenda = {"items": [
        {"id": 0, "text": "Complete Yoma chavrusa (10:00)", "status": "done", "source": "llm"},
        {"id": 1, "text": "Anki review", "status": "missed", "source": "llm"},
        {"id": 2, "text": "Job applications", "status": "done", "source": "llm"},
    ]}
    (tmp_path / f"{today}-agenda.json").write_text(json.dumps(agenda))
    stats = logs.compute_stats(days=1)
    s = stats[str(today)]
    assert s["anchors"] == (1, 2)  # chavrusa+done, anki+missed; job apps not an anchor


def test_compute_stats_wins(tmp_path):
    logs = Logs(str(tmp_path))
    logs.write("win", "shipped a feature")
    logs.write("win", "took a walk")
    logs.write("note", "just a note")
    stats = logs.compute_stats(days=1)
    assert stats[str(date.today())]["wins"] == 2


def test_compute_stats_checkin_response(tmp_path):
    logs = Logs(str(tmp_path))
    today = date.today()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    jsonl = tmp_path / f"{today}.jsonl"
    reminder = {"ts": now.isoformat(timespec="seconds"), "tag": "reminder", "content": "check in"}
    checkin  = {"ts": (now + timedelta(minutes=5)).isoformat(timespec="seconds"), "tag": "checkin", "content": "working"}
    jsonl.write_text(json.dumps(reminder) + "\n" + json.dumps(checkin) + "\n")
    stats = logs.compute_stats(days=1)
    s = stats[str(today)]
    assert s["reminders"] == 1
    assert s["responded"] == 1


def test_format_stats_for_prompt(tmp_path):
    logs = Logs(str(tmp_path))
    today = date.today()
    agenda = {"items": [
        {"id": 0, "text": "Job search", "status": "done", "source": "llm"},
        {"id": 1, "text": "Anki", "status": "done", "source": "llm"},
    ]}
    (tmp_path / f"{today}-agenda.json").write_text(json.dumps(agenda))
    logs.write("win", "applied to 3 jobs")
    text = logs.format_stats_for_prompt(days=1)
    assert "Completion" in text
    assert "Wins" in text
    assert "Rolling" in text


def test_format_metrics_for_prompt(tmp_path):
    logs = Logs(str(tmp_path))
    yesterday = date.today() - timedelta(days=1)
    jsonl = tmp_path / f"{yesterday}.jsonl"
    jsonl.write_text(
        json.dumps({"ts": "2026-05-26T10:00:00+03:00", "tag": "metric", "key": "mood", "value": 8, "unit": "", "content": "mood 8"}) + "\n"
    )
    text = logs.format_metrics_for_prompt(days=2)
    assert "mood" in text
    assert "Tracked metrics:" in text

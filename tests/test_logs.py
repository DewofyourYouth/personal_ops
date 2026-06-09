import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from logs import Logs


@pytest.fixture
def log_dir(tmp_path):
    return Logs(str(tmp_path))


def test_write_and_read_today(log_dir):
    """Logs.write stores an entry that read_today returns with tag and content."""
    log_dir.write("note", "hello world")
    entries = log_dir.read_today()
    assert len(entries) == 1
    assert entries[0]["tag"] == "note"
    assert entries[0]["content"] == "hello world"


def test_write_metric(log_dir):
    """Logs.write_metric stores metrics in SQLite and exposes them via load_metrics."""
    # Metrics live in the SQLite `metrics` table (not `entries`/read_today since
    # the migration); read them back through the public load_metrics API.
    log_dir.write_metric("weight", 75.5, "kg")
    metrics = log_dir.load_metrics(days=1)
    assert "weight" in metrics
    date_str, display = metrics["weight"][0]
    assert display == "75.5kg"


def test_read_recent_skips_metrics(tmp_path):
    """read_recent excludes metric rows from human-facing recent log text."""
    logs = Logs(str(tmp_path))
    yesterday = date.today() - timedelta(days=1)
    jsonl = tmp_path / f"{yesterday}.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "ts": "2026-05-26T10:00:00+03:00",
                "tag": "note",
                "content": "readable entry",
            }
        )
        + "\n"
        + json.dumps(
            {
                "ts": "2026-05-26T10:01:00+03:00",
                "tag": "metric",
                "key": "weight",
                "value": 75,
                "unit": "kg",
                "content": "weight 75kg",
            }
        )
        + "\n"
    )
    recent = logs.read_recent(days=1)
    assert "readable entry" in recent
    assert "metric" not in recent
    assert "weight" not in recent


def test_load_metrics(log_dir):
    """load_metrics groups recent metric values by metric key."""
    log_dir.write_metric("steps", 8000)
    metrics = log_dir.load_metrics(days=2)
    assert "steps" in metrics
    assert int(metrics["steps"][0][1]) == 8000


def test_parse_md_fallback(tmp_path):
    """read_recent can parse legacy markdown log files when JSONL data is absent."""
    logs = Logs(str(tmp_path))
    yesterday = date.today() - timedelta(days=1)
    md = tmp_path / f"{yesterday}.md"
    md.write_text("## 09:00 #insight\nSomething interesting happened\n")
    recent = logs.read_recent(days=1)
    assert "Something interesting happened" in recent


def test_read_recent_no_logs(log_dir):
    """read_recent returns the empty-state message when no logs exist."""
    result = log_dir.read_recent(days=3)
    assert result == "No recent logs."


def test_read_recent_includes_today(log_dir):
    """read_recent includes entries written for the current day."""
    log_dir.write("note", "today's entry")
    result = log_dir.read_recent(days=1)
    assert "today's entry" in result


def test_format_today_for_telegram_reads_sqlite_without_jsonl(tmp_path):
    """The /logs formatter shows entries even when only the SQLite row exists."""
    logs = Logs(str(tmp_path))
    today = datetime.now(ZoneInfo("Asia/Jerusalem")).date()
    logs.db.insert_entry(
        f"{today}T10:15:00+03:00", today.isoformat(), "note", "sqlite-only entry"
    )

    messages = logs.format_today_for_telegram()

    assert not (tmp_path / f"{today}.jsonl").exists()
    assert len(messages) == 1
    assert "sqlite-only entry" in messages[0]
    assert "<code>10:15</code> <b>#note</b>" in messages[0]


def test_format_today_for_telegram_chunks_long_escaped_entries(tmp_path):
    """The /logs formatter chunks safely and still includes entries after long logs."""
    logs = Logs(str(tmp_path))
    today = datetime.now(ZoneInfo("Asia/Jerusalem")).date()
    logs.db.insert_entry(
        f"{today}T10:00:00+03:00",
        today.isoformat(),
        "log",
        "A & B < C " * 80,
    )
    logs.db.insert_entry(
        f"{today}T10:05:00+03:00", today.isoformat(), "note", "later entry"
    )

    messages = logs.format_today_for_telegram(max_chars=180)

    assert all(len(m) <= 180 for m in messages)
    assert any("entry truncated" in m for m in messages)
    assert any("A &amp; B &lt; C" in m for m in messages)
    assert any("later entry" in m for m in messages)


def test_compute_stats_completion(tmp_path):
    """compute_stats counts done and missed agenda items while excluding open items."""
    logs = Logs(str(tmp_path))
    today = date.today()
    agenda = {
        "items": [
            {"id": 0, "text": "Do something", "status": "done", "source": "llm"},
            {"id": 1, "text": "Do another", "status": "missed", "source": "llm"},
            {"id": 2, "text": "Open item", "status": "open", "source": "llm"},
        ]
    }
    (tmp_path / f"{today}-agenda.json").write_text(json.dumps(agenda))
    stats = logs.compute_stats(days=1)
    s = stats[str(today)]
    assert s["completion"] == (1, 2)  # open items excluded


def test_compute_stats_anchors(tmp_path):
    """compute_stats counts completed and missed anchor tasks separately."""
    logs = Logs(str(tmp_path))
    today = date.today()
    agenda = {
        "items": [
            {
                "id": 0,
                "text": "Complete Yoma chavrusa (10:00)",
                "status": "done",
                "source": "llm",
            },
            {"id": 1, "text": "Anki review", "status": "missed", "source": "llm"},
            {"id": 2, "text": "Job applications", "status": "done", "source": "llm"},
        ]
    }
    (tmp_path / f"{today}-agenda.json").write_text(json.dumps(agenda))
    stats = logs.compute_stats(days=1)
    s = stats[str(today)]
    assert s["anchors"] == (1, 2)  # chavrusa+done, anki+missed; job apps not an anchor


def test_compute_stats_wins(tmp_path):
    """compute_stats counts entries tagged as wins for the day."""
    logs = Logs(str(tmp_path))
    logs.write("win", "shipped a feature")
    logs.write("win", "took a walk")
    logs.write("note", "just a note")
    stats = logs.compute_stats(days=1)
    assert stats[str(date.today())]["wins"] == 2


def test_compute_stats_checkin_response(tmp_path):
    """compute_stats counts reminder prompts and nearby checkin responses."""
    logs = Logs(str(tmp_path))
    today = date.today()
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Jerusalem")
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    jsonl = tmp_path / f"{today}.jsonl"
    reminder = {
        "ts": now.isoformat(timespec="seconds"),
        "tag": "reminder",
        "content": "check in",
    }
    checkin = {
        "ts": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
        "tag": "checkin",
        "content": "working",
    }
    jsonl.write_text(json.dumps(reminder) + "\n" + json.dumps(checkin) + "\n")
    stats = logs.compute_stats(days=1)
    s = stats[str(today)]
    assert s["reminders"] == 1
    assert s["responded"] == 1


def test_format_stats_for_prompt(tmp_path):
    """format_stats_for_prompt renders completion, wins, and rolling stats sections."""
    logs = Logs(str(tmp_path))
    today = date.today()
    agenda = {
        "items": [
            {"id": 0, "text": "Job search", "status": "done", "source": "llm"},
            {"id": 1, "text": "Anki", "status": "done", "source": "llm"},
        ]
    }
    (tmp_path / f"{today}-agenda.json").write_text(json.dumps(agenda))
    logs.write("win", "applied to 3 jobs")
    text = logs.format_stats_for_prompt(days=1)
    assert "Completion" in text
    assert "Wins" in text
    assert "Rolling" in text


def test_format_metrics_for_prompt(log_dir):
    """format_metrics_for_prompt renders tracked metric summaries."""
    log_dir.write_metric("mood", 8)
    text = log_dir.format_metrics_for_prompt(days=2)
    assert "mood" in text
    assert "Tracked metrics:" in text


def test_mood_energy_by_time_of_day(tmp_path):
    """mood_energy_by_time_of_day buckets mood and energy averages by local hour."""
    # Write readings at known hours via JSONL (fresh DB → JSONL fallback fires).
    logs = Logs(str(tmp_path))
    today = date.today()
    rows = [
        ("08:00:00", "mood", 4),
        ("08:01:00", "energy", 3),  # morning
        ("14:00:00", "mood", 2),
        ("14:01:00", "energy", 1),  # afternoon
        ("20:00:00", "mood", 3),
        ("20:01:00", "energy", 2),  # evening
    ]
    jsonl = tmp_path / f"{today}.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": f"{today}T{t}+03:00",
                    "tag": "metric",
                    "key": k,
                    "value": v,
                    "content": f"{k} {v}",
                }
            )
            for t, k, v in rows
        )
        + "\n"
    )
    tod = logs.mood_energy_by_time_of_day(days=1)
    assert tod["morning"]["mood_avg"] == 4.0
    assert tod["morning"]["energy_avg"] == 3.0
    assert tod["afternoon"]["mood_avg"] == 2.0
    assert tod["evening"]["mood_avg"] == 3.0
    # Empty buckets are omitted entirely.
    assert "late night" not in tod


def test_read_skips_reminder_noise(tmp_path):
    """read_recent hides bot reminder prompts while keeping real user content."""
    # Reminder entries are bot-fired prompt noise — they must not appear in the
    # human/LLM-facing day read (regression: they were 31% of all entries).
    logs = Logs(str(tmp_path))
    logs.write("reminder", "What are you doing? Log it with: checkin <activity>")
    logs.write("note", "real content")
    text = logs.read_recent(days=1)
    assert "real content" in text
    assert "What are you doing" not in text

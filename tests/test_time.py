import sys
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from logs import TZ, Logs, _parse_time_entry
from time_handlers import (
    REPORT_PERIOD_DAYS,
    TimeHandlers,
    _format_duration,
    _parse_duration_prefix,
    _parse_project,
    _time_log_content,
    _time_report,
)
from time_tracker import TimeTracker

_ALLOWED = 123


def _make_logs() -> Logs:
    return Logs(tempfile.mkdtemp())


def _make_handler(logs: Logs | None = None) -> TimeHandlers:
    logs = logs or _make_logs()
    tracker = TimeTracker(logs.db)
    return TimeHandlers(AsyncMock(), logs, tracker, _ALLOWED)


def _update(args=None):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=_ALLOWED),
        effective_chat=types.SimpleNamespace(id=_ALLOWED),
        message=types.SimpleNamespace(reply_text=AsyncMock()),
    ), types.SimpleNamespace(args=args or [])


def _reply_text(update) -> str:
    return update.message.reply_text.await_args.args[0]


# --- Pure parsing: duration prefix ---


def test_parse_duration_prefix_hours_and_minutes():
    assert _parse_duration_prefix("1h30m job apps") == (90.0, "job apps")


def test_parse_duration_prefix_plain_hours():
    assert _parse_duration_prefix("2h deep work") == (120.0, "deep work")


def test_parse_duration_prefix_decimal_hours():
    assert _parse_duration_prefix("1.5h client call") == (90.0, "client call")


def test_parse_duration_prefix_word_forms():
    assert _parse_duration_prefix("2 hours reviewing contract") == (
        120.0,
        "reviewing contract",
    )
    assert _parse_duration_prefix("90 min deep work") == (90.0, "deep work")
    assert _parse_duration_prefix("45m writing") == (45.0, "writing")


def test_parse_duration_prefix_rejects_bare_number():
    """A leading number with no unit must never be read as a duration — it's
    ambiguous with the description itself (e.g. '2 job apps' isn't '2 hours')."""
    assert _parse_duration_prefix("2 job apps") is None


def test_parse_duration_prefix_rejects_no_description():
    assert _parse_duration_prefix("1h30m") is None
    assert _parse_duration_prefix("2h   ") is None


def test_parse_duration_prefix_rejects_empty_string():
    assert _parse_duration_prefix("") is None


# --- Pure parsing: project tag ---


def test_parse_project_extracts_and_normalizes():
    assert _parse_project("client call #Acme") == ("client call", "acme")


def test_parse_project_mid_sentence():
    assert _parse_project("reviewing #acme contract") == (
        "reviewing contract",
        "acme",
    )


def test_parse_project_none_found():
    assert _parse_project("deep work on the report") == (
        "deep work on the report",
        "",
    )


# --- Formatting / round-trip through the parseable content line ---


@pytest.mark.parametrize(
    "minutes,expected",
    [(45, "45m"), (60, "1h"), (90, "1h 30m"), (150, "2h 30m")],
)
def test_format_duration(minutes, expected):
    assert _format_duration(minutes) == expected


def test_time_log_content_round_trips_with_project():
    content = _time_log_content("client call", 90, "acme")
    assert content == "client call — 1h 30m [project: acme]"
    parsed = _parse_time_entry(content)
    assert parsed == {"minutes": 90.0, "project": "acme"}


def test_time_log_content_round_trips_without_project():
    content = _time_log_content("deep work", 45)
    assert content == "deep work — 45m"
    parsed = _parse_time_entry(content)
    assert parsed == {"minutes": 45.0, "project": ""}


def test_parse_time_entry_none_for_unparseable_content():
    assert _parse_time_entry("just a plain log entry") is None


# --- TimeTracker (live-timer state) ---


def test_tracker_start_running_stop_happy_path():
    tracker = TimeTracker(_make_logs().db)
    assert tracker.running(1) is None
    tracker.start(1, "reviewing contract", "acme")
    running = tracker.running(1)
    assert running["description"] == "reviewing contract"
    assert running["project"] == "acme"

    # Rewind start_ts to simulate real elapsed time without sleeping in a test.
    tracker.db.execute(
        "UPDATE time_running SET start_ts = ? WHERE chat_id = ?",
        ((_now_minus(30)).isoformat(), 1),
    )
    result = tracker.stop(1)
    assert result["description"] == "reviewing contract"
    assert result["project"] == "acme"
    assert 29 <= result["minutes"] <= 31
    assert tracker.running(1) is None


def test_tracker_stop_returns_none_when_nothing_running():
    tracker = TimeTracker(_make_logs().db)
    assert tracker.stop(1) is None


def test_tracker_cancel_returns_none_when_nothing_running():
    tracker = TimeTracker(_make_logs().db)
    assert tracker.cancel(1) is None


def test_tracker_cancel_discards_without_logging_state():
    tracker = TimeTracker(_make_logs().db)
    tracker.start(1, "deep work")
    cancelled = tracker.cancel(1)
    assert cancelled["description"] == "deep work"
    assert tracker.running(1) is None


def test_tracker_start_replaces_existing_running_timer():
    """The service itself doesn't guard against a double-start (the handler
    does) — confirm it stays sane (no crash, last start wins) if called anyway."""
    tracker = TimeTracker(_make_logs().db)
    tracker.start(1, "first thing")
    tracker.start(1, "second thing")
    assert tracker.running(1)["description"] == "second thing"


def _now_minus(minutes: int):
    from datetime import datetime

    return datetime.now(TZ) - timedelta(minutes=minutes)


# --- Handler: live timer commands ---


@pytest.mark.asyncio
async def test_cmd_timer_start_then_stop_logs_one_entry():
    logs = _make_logs()
    handler = _make_handler(logs)
    update, context = _update(["reviewing", "contract", "#acme"])
    await handler.cmd_timer_start(update, context)
    assert "Timer started" in _reply_text(update)

    # Simulate 30 minutes elapsed before stopping.
    handler.time_tracker.db.execute(
        "UPDATE time_running SET start_ts = ? WHERE chat_id = ?",
        (_now_minus(30).isoformat(), _ALLOWED),
    )
    update2, context2 = _update()
    await handler.cmd_timer_stop(update2, context2)
    assert "Logged" in _reply_text(update2)

    entries = [e for e in logs.read_today() if e.get("tag") == "time"]
    assert len(entries) == 1
    parsed = _parse_time_entry(entries[0]["content"])
    assert parsed["project"] == "acme"
    assert 29 <= parsed["minutes"] <= 31


@pytest.mark.asyncio
async def test_cmd_timer_start_warns_when_already_running_and_does_not_clobber():
    handler = _make_handler()
    update, context = _update(["first", "thing"])
    await handler.cmd_timer_start(update, context)

    update2, context2 = _update(["second", "thing"])
    await handler.cmd_timer_start(update2, context2)
    assert "Already timing" in _reply_text(update2)
    assert handler.time_tracker.running(_ALLOWED)["description"] == "first thing"


@pytest.mark.asyncio
async def test_cmd_timer_stop_with_nothing_running_replies_gracefully():
    handler = _make_handler()
    update, context = _update()
    await handler.cmd_timer_stop(update, context)
    assert "No timer running" in _reply_text(update)


@pytest.mark.asyncio
async def test_cmd_timer_stop_under_a_minute_not_logged():
    logs = _make_logs()
    handler = _make_handler(logs)
    update, context = _update(["quick", "thing"])
    await handler.cmd_timer_start(update, context)

    update2, context2 = _update()
    await handler.cmd_timer_stop(update2, context2)
    assert "under a minute" in _reply_text(update2)
    assert [e for e in logs.read_today() if e.get("tag") == "time"] == []


@pytest.mark.asyncio
async def test_cmd_timer_status_shows_elapsed():
    handler = _make_handler()
    update, context = _update(["deep", "work", "#writing"])
    await handler.cmd_timer_start(update, context)
    handler.time_tracker.db.execute(
        "UPDATE time_running SET start_ts = ? WHERE chat_id = ?",
        (_now_minus(10).isoformat(), _ALLOWED),
    )
    update2, context2 = _update()
    await handler.cmd_timer_status(update2, context2)
    reply = _reply_text(update2)
    assert "deep work" in reply
    assert "writing" in reply


@pytest.mark.asyncio
async def test_cmd_timer_status_nothing_running():
    handler = _make_handler()
    update, context = _update()
    await handler.cmd_timer_status(update, context)
    assert "No timer running" in _reply_text(update)


@pytest.mark.asyncio
async def test_cmd_timer_cancel_discards_without_logging():
    logs = _make_logs()
    handler = _make_handler(logs)
    update, context = _update(["deep", "work"])
    await handler.cmd_timer_start(update, context)

    update2, context2 = _update()
    await handler.cmd_timer_cancel(update2, context2)
    assert "Discarded" in _reply_text(update2)
    assert handler.time_tracker.running(_ALLOWED) is None
    assert [e for e in logs.read_today() if e.get("tag") == "time"] == []


@pytest.mark.asyncio
async def test_cmd_timer_cancel_nothing_running():
    handler = _make_handler()
    update, context = _update()
    await handler.cmd_timer_cancel(update, context)
    assert "No timer running" in _reply_text(update)


# --- Handler: time: prefix (retrospective log) ---


@pytest.mark.asyncio
async def test_handle_classified_text_ignores_other_tags():
    handler = _make_handler()
    reply = AsyncMock()
    handled = await handler.handle_classified_text("food", "2h lasagna", reply)
    assert handled is False
    reply.assert_not_called()


@pytest.mark.asyncio
async def test_handle_classified_text_valid_duration_logs_entry():
    logs = _make_logs()
    handler = _make_handler(logs)
    reply = AsyncMock()
    handled = await handler.handle_classified_text(
        "time", "1h30m client call #acme", reply
    )
    assert handled is True
    reply.assert_awaited_once()
    entries = [e for e in logs.read_today() if e.get("tag") == "time"]
    assert len(entries) == 1
    assert _parse_time_entry(entries[0]["content"]) == {
        "minutes": 90.0,
        "project": "acme",
    }


@pytest.mark.asyncio
async def test_handle_classified_text_invalid_duration_does_not_log():
    logs = _make_logs()
    handler = _make_handler(logs)
    reply = AsyncMock()
    handled = await handler.handle_classified_text("time", "job applications", reply)
    assert handled is True
    reply.assert_awaited_once()
    assert "duration" in reply.await_args.args[0].lower()
    assert [e for e in logs.read_today() if e.get("tag") == "time"] == []


@pytest.mark.asyncio
async def test_handle_classified_text_duration_with_no_description_does_not_log():
    logs = _make_logs()
    handler = _make_handler(logs)
    reply = AsyncMock()
    handled = await handler.handle_classified_text("time", "1h30m", reply)
    assert handled is True
    assert [e for e in logs.read_today() if e.get("tag") == "time"] == []


# --- /timereport ---


@pytest.mark.asyncio
async def test_cmd_time_report_rejects_invalid_period():
    handler = _make_handler()
    update, context = _update(["fortnight"])
    await handler.cmd_time_report(update, context)
    assert "/timereport week" in _reply_text(update)


def test_report_periods_are_rolling_windows():
    assert REPORT_PERIOD_DAYS["week"] == 7
    assert REPORT_PERIOD_DAYS["month"] == 30


def test_time_report_totals_and_by_project_breakdown():
    today = date.today()
    ts = today.isoformat() + "T09:00:00+03:00"
    entries = [
        {
            "date": today.isoformat(),
            "ts": ts,
            "content": _time_log_content("client call", 90, "acme"),
        },
        {
            "date": today.isoformat(),
            "ts": ts,
            "content": _time_log_content("deep work", 45),
        },
    ]
    report = _time_report(entries, "week", today)
    assert "2h 15m" in report  # total
    assert "acme" in report
    assert "(no project)" in report
    assert "client call" in report
    assert "deep work" in report


def test_time_report_handles_no_time_logged():
    report = _time_report([], "week", date.today())
    assert "No time was logged" in report


# --- Trackable.summary ---


def test_summary_empty_when_no_time_logged():
    handler = _make_handler()
    assert handler.summary(7) == ""


def test_summary_reports_total_and_entry_count():
    logs = _make_logs()
    handler = _make_handler(logs)
    logs.write("time", _time_log_content("client call", 90, "acme"))
    logs.write("time", _time_log_content("deep work", 45))
    summary = handler.summary(7)
    assert "2h 15m" in summary
    assert "2 entries" in summary

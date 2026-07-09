"""Tests for EOD reconciliation — 22:15 check, grace-cutoff auto-miss, quiet-window exclusion."""

import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

_TZ_STR = "Asia/Jerusalem"


def _dt(iso: str) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.fromisoformat(iso).astimezone(ZoneInfo(_TZ_STR))


def _make_store(habits: list[dict] | None = None):
    store = MagicMock()
    store.sections.return_value = {"Morning": habits} if habits else {}
    return store


def _make_logs(entries: list[dict] | None = None):
    logs = MagicMock()
    logs.read_today.return_value = entries or []
    logs.log_dir = "/tmp"
    db = MagicMock()
    db.entries_for_date.return_value = entries or []
    logs.db = db
    logs.write = MagicMock()
    return logs


def _make_context():
    ctx = MagicMock()
    ctx.habit_display_name.side_effect = lambda name: name
    return ctx


def _make_quiet_window(is_quiet: bool = False):
    qw = MagicMock()
    qw.is_quiet_at.return_value = is_quiet
    return qw


def _make_habit_handlers(habits=None, entries=None, is_quiet=False):
    """Build a HabitHandlers instance with minimal mocked dependencies."""
    from habit_handlers import HabitHandlers

    bot = AsyncMock()
    logs = _make_logs(entries)
    ctx = _make_context()
    qw = _make_quiet_window(is_quiet)

    handler = HabitHandlers.__new__(HabitHandlers)
    handler.bot = bot
    handler.logs = logs
    handler.context = ctx
    handler.allowed_user = 12345
    handler.planner = None
    handler.shabbat = MagicMock()
    handler.quiet_window = qw

    store = _make_store(habits)
    handler.store = store

    return handler


class TestEodMessagePending:
    def test_returns_none_when_all_habits_logged(self):
        habits = [{"id": 1, "name": "Drink water", "days": None}]
        entries = [
            {
                "tag": "habit",
                "content": "Drink water",
                "ts": "2026-06-22T09:00:00+03:00",
            }
        ]
        h = _make_habit_handlers(habits=habits, entries=entries)
        text, keyboard = h._eod_message()
        assert text is None
        assert keyboard is None

    def test_returns_message_when_habits_pending(self):
        habits = [{"id": 1, "name": "Drink water", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[])  # nothing logged
        text, keyboard = h._eod_message()
        assert text is not None
        assert "Drink water" in text

    def test_for_date_shows_yesterday_label(self):
        yesterday = date.today()
        from datetime import timedelta

        yesterday = date.today() - timedelta(days=1)
        habits = [{"id": 1, "name": "Drink water", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[])
        text, _ = h._eod_message(for_date=yesterday)
        assert text is not None
        assert "yesterday" in text

    def test_today_label_when_no_for_date(self):
        habits = [{"id": 1, "name": "Exercise", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[])
        text, _ = h._eod_message()
        assert "today" in text


class TestAutoMissPending:
    @pytest.mark.asyncio
    async def test_auto_miss_logs_pending_as_missed(self):
        habits = [{"id": 1, "name": "Meditate", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[])

        await h.auto_miss_pending()

        h.logs.write.assert_called_once_with("habit_missed", "Meditate")

    @pytest.mark.asyncio
    async def test_auto_miss_sends_notification(self):
        habits = [{"id": 1, "name": "Meditate", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[])

        await h.auto_miss_pending()

        h.bot.send_message.assert_called_once()
        call_kwargs = h.bot.send_message.call_args.kwargs
        assert "Meditate" in call_kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_auto_miss_skips_when_quiet_window(self):
        habits = [{"id": 1, "name": "Meditate", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[], is_quiet=True)

        await h.auto_miss_pending()

        h.logs.write.assert_not_called()
        h.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_miss_silent_when_nothing_pending(self):
        habits = [{"id": 1, "name": "Meditate", "days": None}]
        entries = [
            {"tag": "habit", "content": "Meditate", "ts": "2026-06-22T09:00:00+03:00"}
        ]
        h = _make_habit_handlers(habits=habits, entries=entries)

        await h.auto_miss_pending()

        h.logs.write.assert_not_called()
        h.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_miss_marks_all_pending_habits(self):
        habits = [
            {"id": 1, "name": "Meditate", "days": None},
            {"id": 2, "name": "Exercise", "days": None},
        ]
        h = _make_habit_handlers(habits=habits, entries=[])

        await h.auto_miss_pending()

        assert h.logs.write.call_count == 2
        calls = [c.args[0] for c in h.logs.write.call_args_list]
        assert all(tag == "habit_missed" for tag in calls)


class TestDailyHabitCheckQuietWindow:
    @pytest.mark.asyncio
    async def test_skips_during_quiet_window(self):
        habits = [{"id": 1, "name": "Exercise", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[], is_quiet=True)

        await h.daily_habit_check()

        h.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_bypasses_quiet_window(self):
        habits = [{"id": 1, "name": "Exercise", "days": None}]
        h = _make_habit_handlers(habits=habits, entries=[], is_quiet=True)

        # Patch send_sticker to avoid its actual call
        with patch("habit_handlers.send_sticker", new=AsyncMock()):
            await h.daily_habit_check(force=True)

        h.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_silent_when_all_done(self):
        habits = [{"id": 1, "name": "Exercise", "days": None}]
        entries = [
            {"tag": "habit", "content": "Exercise", "ts": "2026-06-22T09:00:00+03:00"}
        ]
        h = _make_habit_handlers(habits=habits, entries=entries, is_quiet=False)

        with patch("habit_handlers.send_sticker", new=AsyncMock()):
            await h.daily_habit_check()

        h.bot.send_message.assert_not_called()


class TestJobTimings:
    def test_eod_check_is_at_22_15(self):
        from habit_handlers import HabitHandlers

        # Minimal construction just to inspect jobs
        bot = AsyncMock()
        logs = _make_logs()
        ctx = _make_context()

        with patch("habit_handlers.HabitStore", return_value=MagicMock()):
            with patch("habit_handlers.Shabbat", return_value=MagicMock()):
                with patch(
                    "habit_handlers._make_quiet_window", return_value=MagicMock()
                ):
                    h = HabitHandlers(bot, logs, ctx, 12345)

        eod_job = next(j for j in h.jobs if j["id"] == "habit_eod_check")
        assert eod_job["kwargs"]["hour"] == 22
        assert eod_job["kwargs"]["minute"] == 15

    def test_auto_miss_is_at_22_45(self):
        from habit_handlers import HabitHandlers

        bot = AsyncMock()
        logs = _make_logs()
        ctx = _make_context()

        with patch("habit_handlers.HabitStore", return_value=MagicMock()):
            with patch("habit_handlers.Shabbat", return_value=MagicMock()):
                with patch(
                    "habit_handlers._make_quiet_window", return_value=MagicMock()
                ):
                    h = HabitHandlers(bot, logs, ctx, 12345)

        auto_miss_job = next(j for j in h.jobs if j["id"] == "habit_eod_auto_miss")
        assert auto_miss_job["kwargs"]["hour"] == 22
        assert auto_miss_job["kwargs"]["minute"] == 45

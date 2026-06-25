"""Tests for StalenessChecker — staleness-trigger logic."""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

_TZ_STR = "Asia/Jerusalem"


def _dt(iso: str) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(_TZ_STR))


def _make_db(last_entry_ts: str | None = None, last_prompted_ts: str | None = None):
    """Stub DB: query returns entry/prompt rows based on args."""
    db = MagicMock()

    def query_side_effect(sql, params=()):
        if "FROM entries" in sql:
            if last_entry_ts:
                row = MagicMock()
                row.__getitem__ = lambda _, k: last_entry_ts if k == "ts" else None
                return [row]
            return []
        if "FROM staleness_prompts" in sql:
            if last_prompted_ts:
                row = MagicMock()
                row.__getitem__ = lambda _, k: last_prompted_ts if k == "last_prompted_at" else None
                return [row]
            return []
        return []

    db.query.side_effect = query_side_effect
    db.execute = MagicMock()
    db.ensure_schema = MagicMock()
    return db


def _make_quiet_window(should_prompt: bool = True):
    qw = MagicMock()
    qw.should_prompt.return_value = should_prompt
    return qw


class TestStaleTracks:
    def _checker(
        self,
        last_entry_ts=None,
        last_prompted_ts=None,
        should_prompt=True,
        config=None,
    ):
        from staleness import StalenessChecker

        db = _make_db(last_entry_ts, last_prompted_ts)
        qw = _make_quiet_window(should_prompt)
        checker = StalenessChecker(db, qw)
        if config is not None:
            checker._config = config
        return checker

    def test_no_entries_and_no_prompt_is_stale(self):
        checker = self._checker(last_entry_ts=None, last_prompted_ts=None)
        assert "checkin" in checker.stale_tracks()

    def test_recent_entry_is_not_stale(self):
        # Entry logged 1 hour ago; threshold is 4h → not stale
        recent = (datetime.now(__import__("zoneinfo").ZoneInfo(_TZ_STR)) - timedelta(hours=1)).isoformat()
        checker = self._checker(last_entry_ts=recent)
        assert checker.stale_tracks() == []

    def test_old_entry_is_stale(self):
        from zoneinfo import ZoneInfo
        old = (datetime.now(ZoneInfo(_TZ_STR)) - timedelta(hours=5)).isoformat()
        checker = self._checker(last_entry_ts=old)
        assert "checkin" in checker.stale_tracks()

    def test_recently_prompted_suppresses_nudge(self):
        from zoneinfo import ZoneInfo
        # Both entry and prompt are old, but prompt was recent enough
        old_entry = (datetime.now(ZoneInfo(_TZ_STR)) - timedelta(hours=5)).isoformat()
        recent_prompt = (datetime.now(ZoneInfo(_TZ_STR)) - timedelta(hours=1)).isoformat()
        checker = self._checker(last_entry_ts=old_entry, last_prompted_ts=recent_prompt)
        assert checker.stale_tracks() == []

    def test_old_prompt_allows_new_nudge(self):
        from zoneinfo import ZoneInfo
        old_entry = (datetime.now(ZoneInfo(_TZ_STR)) - timedelta(hours=6)).isoformat()
        old_prompt = (datetime.now(ZoneInfo(_TZ_STR)) - timedelta(hours=5)).isoformat()
        checker = self._checker(last_entry_ts=old_entry, last_prompted_ts=old_prompt)
        assert "checkin" in checker.stale_tracks()

    def test_quiet_window_suppresses_all_tracks(self):
        checker = self._checker(last_entry_ts=None, should_prompt=False)
        assert checker.stale_tracks() == []

    def test_custom_threshold_respected(self):
        from zoneinfo import ZoneInfo
        # Entry 3 hours ago; custom threshold = 2h → stale
        entry = (datetime.now(ZoneInfo(_TZ_STR)) - timedelta(hours=3)).isoformat()
        checker = self._checker(last_entry_ts=entry, config={"checkin": 2})
        assert "checkin" in checker.stale_tracks()

    def test_custom_threshold_not_yet_exceeded(self):
        from zoneinfo import ZoneInfo
        # Entry 1 hour ago; custom threshold = 2h → not stale
        entry = (datetime.now(ZoneInfo(_TZ_STR)) - timedelta(hours=1)).isoformat()
        checker = self._checker(last_entry_ts=entry, config={"checkin": 2})
        assert checker.stale_tracks() == []


class TestCheckAndPrompt:
    @pytest.mark.asyncio
    async def test_sends_message_for_stale_track(self):
        from staleness import StalenessChecker

        db = _make_db()  # no entries → stale
        qw = _make_quiet_window(should_prompt=True)
        checker = StalenessChecker(db, qw)

        bot = AsyncMock()
        await checker.check_and_prompt(bot, chat_id=12345)

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 12345

    @pytest.mark.asyncio
    async def test_no_message_when_not_quiet_window(self):
        from staleness import StalenessChecker

        db = _make_db()
        qw = _make_quiet_window(should_prompt=False)
        checker = StalenessChecker(db, qw)

        bot = AsyncMock()
        await checker.check_and_prompt(bot, chat_id=12345)

        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_records_prompt_time_after_sending(self):
        from staleness import StalenessChecker

        db = _make_db()
        qw = _make_quiet_window(should_prompt=True)
        checker = StalenessChecker(db, qw)

        bot = AsyncMock()
        await checker.check_and_prompt(bot, chat_id=12345)

        db.execute.assert_called()


class TestConfigLoading:
    def test_config_file_loaded(self):
        from staleness import StalenessChecker

        config = {"checkin": 6, "food": 8}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(config, f)
            path = f.name

        db = _make_db()
        qw = _make_quiet_window()
        checker = StalenessChecker(db, qw, config_path=path)

        assert checker._config["checkin"] == 6
        assert checker._config["food"] == 8

    def test_missing_config_uses_defaults(self):
        from staleness import StalenessChecker

        db = _make_db()
        qw = _make_quiet_window()
        checker = StalenessChecker(db, qw, config_path="/nonexistent/config.json")

        assert "checkin" in checker._config

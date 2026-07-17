"""Regression: the recurring check-in nudge fired one minute after a #checkin
was logged (voice note → "Logged #checkin ✓" → "What are you doing?" nudge).

A check-in reminder due to fire must be skipped when a #checkin entry landed
within the reminder's own interval (45-min fallback for non-interval ones).
Non-check-in reminders are unaffected.
"""

import asyncio
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from reminder_handlers import ReminderHandlers

_TZ = ZoneInfo("Asia/Jerusalem")

NUDGE = {
    "id": "1",
    "text": "What are you doing? Log it with: checkin <activity>",
    "type": "interval",
    "interval_minutes": 45,
}
PLAIN = {"id": "2", "text": "take out the trash", "type": "daily", "time": "12:00"}


def _handlers(due, last_checkin_ts):
    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs["text"])

    h = ReminderHandlers.__new__(ReminderHandlers)
    h.bot = types.SimpleNamespace(send_message=send_message)
    h.allowed_user = 1
    h.shabbat = types.SimpleNamespace(quiet_now=lambda: False)
    h.reminders = types.SimpleNamespace(due_now=lambda: due)
    rows = [{"ts": last_checkin_ts}] if last_checkin_ts else []
    h.logs = types.SimpleNamespace(db=types.SimpleNamespace(query=lambda sql: rows))
    return h, sent


def _ts(minutes_ago: int) -> str:
    return (datetime.now(_TZ) - timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds"
    )


def test_nudge_suppressed_when_checkin_is_fresh():
    h, sent = _handlers([NUDGE], _ts(minutes_ago=1))
    asyncio.run(h.run_due_check())
    assert sent == []


def test_nudge_fires_when_last_checkin_is_stale():
    h, sent = _handlers([NUDGE], _ts(minutes_ago=46))
    asyncio.run(h.run_due_check())
    assert len(sent) == 1


def test_nudge_fires_when_no_checkin_ever_logged():
    h, sent = _handlers([NUDGE], None)
    asyncio.run(h.run_due_check())
    assert len(sent) == 1


def test_non_checkin_reminder_unaffected_by_fresh_checkin():
    h, sent = _handlers([PLAIN], _ts(minutes_ago=1))
    asyncio.run(h.run_due_check())
    assert len(sent) == 1


def test_suppression_window_uses_the_reminders_own_interval():
    """A 90-minute nudge stays quiet 60 minutes after a check-in; a 45-minute
    one would fire."""
    slow = dict(NUDGE, interval_minutes=90)
    h, sent = _handlers([slow], _ts(minutes_ago=60))
    asyncio.run(h.run_due_check())
    assert sent == []
    h, sent = _handlers([NUDGE], _ts(minutes_ago=60))
    asyncio.run(h.run_due_check())
    assert len(sent) == 1

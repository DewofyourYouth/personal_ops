"""Tests for WeeklyGoals (the JSON-backed store) and WeeklyGoalsHandlers'
Delete/Roll Over button flow — the Sunday clearing session for time-bounded
weekly focus goals.
"""

import asyncio
import sys
import types
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from weekly_goals import WeeklyGoals, WeeklyGoalsHandlers, _week_start


def test_week_start_is_the_sunday_starting_the_week():
    # 2026-08-28 is a Friday (per this session's own log entries).
    friday = date(2026, 8, 28)
    assert _week_start(friday) == date(2026, 8, 23)  # the preceding Sunday
    sunday = date(2026, 8, 23)
    assert _week_start(sunday) == sunday  # a Sunday is its own week start
    saturday = date(2026, 8, 29)
    assert _week_start(saturday) == date(2026, 8, 23)


def test_add_and_active(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    entry = store.add("Finish the Haki debugging")
    assert entry["text"] == "Finish the Haki debugging"
    assert entry["status"] == "active"
    assert store.active() == [entry]


def test_delete_marks_deleted_not_removed(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    entry = store.add("Catch up on job applications")
    assert store.delete(entry["id"]) is True
    assert store.active() == []
    # Soft delete — the row survives, just not "active".
    row = store.get(entry["id"])
    assert row is not None
    assert row["status"] == "deleted"


def test_delete_unknown_id_returns_false(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    assert store.delete("nope1234") is False


def test_delete_already_deleted_item_returns_false(tmp_path):
    """The idempotency guard's data-layer half: deleting twice must not
    silently succeed the second time."""
    store = WeeklyGoals(str(tmp_path))
    entry = store.add("Ship the thing")
    assert store.delete(entry["id"]) is True
    assert store.delete(entry["id"]) is False


def test_roll_over_bumps_week_of_and_stays_active(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    entry = store.add("Advance Shami Arabic")
    before = date.fromisoformat(entry["week_of"])
    assert store.roll_over(entry["id"]) is True
    row = store.get(entry["id"])
    assert row["status"] == "active"
    assert date.fromisoformat(row["week_of"]) == before + timedelta(days=7)


def test_format_for_prompt_empty_when_nothing_active(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    assert store.format_for_prompt() == ""


def test_format_for_prompt_lists_active_goals(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    store.add("Finish the Haki debugging")
    store.add("Catch up on job applications")
    rendered = store.format_for_prompt()
    assert "## This week's focus" in rendered
    assert "Finish the Haki debugging" in rendered
    assert "Catch up on job applications" in rendered


# --- WeeklyGoalsHandlers.handle_goal_review ---


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = types.SimpleNamespace(id=1)
        self.edited = None

    async def answer(self, text=""):
        pass

    async def edit_message_text(self, text, parse_mode=None):
        self.edited = text


def _tap(handlers: WeeklyGoalsHandlers, data: str) -> _FakeQuery:
    query = _FakeQuery(data)
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(handlers.handle_goal_review(update, None))
    return query


def test_delete_button_removes_goal(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    entry = store.add("Finish the Haki debugging")
    handlers = WeeklyGoalsHandlers(bot=None, weekly_goals=store, allowed_user=1)
    query = _tap(handlers, f"wg_delete:{entry['id']}")
    assert "Removed" in query.edited
    assert store.active() == []


def test_rollover_button_keeps_goal_active(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    entry = store.add("Advance Shami Arabic")
    handlers = WeeklyGoalsHandlers(bot=None, weekly_goals=store, allowed_user=1)
    query = _tap(handlers, f"wg_rollover:{entry['id']}")
    assert "Rolled over" in query.edited
    assert len(store.active()) == 1


def test_tapping_delete_twice_does_not_double_apply(tmp_path):
    """Regression-shaped: mirrors the habit-suggestion idempotency fix from
    this session — a stale/duplicate button tap must not re-apply or crash."""
    store = WeeklyGoals(str(tmp_path))
    entry = store.add("Finish the Haki debugging")
    handlers = WeeklyGoalsHandlers(bot=None, weekly_goals=store, allowed_user=1)
    _tap(handlers, f"wg_delete:{entry['id']}")
    second = _tap(handlers, f"wg_delete:{entry['id']}")
    assert second.edited == "Already handled."


def test_unknown_id_is_handled_gracefully(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    handlers = WeeklyGoalsHandlers(bot=None, weekly_goals=store, allowed_user=1)
    query = _tap(handlers, "wg_delete:doesnotexist")
    assert query.edited == "Already handled."


async def _send_weekly_review_messages(handlers, sent):
    async def fake_send_message(chat_id, text, parse_mode=None, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
        return types.SimpleNamespace()

    handlers.bot = types.SimpleNamespace(send_message=fake_send_message)
    await handlers.send_weekly_review()


def test_send_weekly_review_sends_nothing_when_no_active_goals(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    handlers = WeeklyGoalsHandlers(bot=None, weekly_goals=store, allowed_user=1)
    sent = []
    asyncio.run(_send_weekly_review_messages(handlers, sent))
    assert sent == []


def test_send_weekly_review_sends_one_message_per_active_goal(tmp_path):
    store = WeeklyGoals(str(tmp_path))
    store.add("Finish the Haki debugging")
    store.add("Catch up on job applications")
    handlers = WeeklyGoalsHandlers(bot=None, weekly_goals=store, allowed_user=1)
    sent = []
    asyncio.run(_send_weekly_review_messages(handlers, sent))
    assert len(sent) == 2

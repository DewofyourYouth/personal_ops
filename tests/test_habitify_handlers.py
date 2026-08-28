from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

from context import Context
from habit_handlers import HabitHandlers, HabitStore
from habitify import HabitifyError
from logs import Logs


def _handlers_with_store(tmp_path) -> HabitHandlers:
    h = HabitHandlers.__new__(HabitHandlers)
    h.logs = Logs(str(tmp_path))
    h.context = Context(tmp_path)
    h.store = HabitStore(h.logs.db, h.context)
    return h


@pytest.mark.asyncio
async def test_completion_is_sent_to_habitify_before_local_log():
    order = []
    sync = MagicMock()
    sync.complete.side_effect = lambda *args: order.append(("remote", *args))
    logs = MagicMock()
    logs.write.side_effect = lambda *args, **kwargs: order.append(("local", *args)) or 7
    handler = HabitHandlers.__new__(HabitHandlers)
    handler.habitify_sync = sync
    handler.logs = logs
    when = datetime(2026, 8, 27, 22, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))

    entry_id = await handler.record_habit_completion("Shacharit", when=when)

    assert entry_id == 7
    assert order == [
        ("remote", "Shacharit", "2026-08-27"),
        ("local", "habit", "Shacharit"),
    ]
    assert logs.write.call_args.kwargs["when"] == when


@pytest.mark.asyncio
async def test_remote_failure_does_not_create_local_completion():
    sync = MagicMock()
    sync.complete.side_effect = HabitifyError(503, "unavailable")
    logs = MagicMock()
    handler = HabitHandlers.__new__(HabitHandlers)
    handler.habitify_sync = sync
    handler.logs = logs

    with pytest.raises(HabitifyError):
        await handler.record_habit_completion("Shacharit")

    logs.write.assert_not_called()


@pytest.mark.asyncio
async def test_no_api_key_mode_keeps_local_only_behavior():
    logs = MagicMock()
    logs.write.return_value = 11
    handler = HabitHandlers.__new__(HabitHandlers)
    handler.habitify_sync = None
    handler.logs = logs

    assert await handler.record_habit_completion("Shacharit") == 11
    logs.write.assert_called_once()


# --- add_habit_from_text: a habit created here must never end up local-only,
# since sync_from_habitify's reconciliation only leaves alone habits it knows
# are Habitify's (see the untrack-loop tests below). ---


@pytest.mark.asyncio
async def test_add_habit_from_text_creates_in_habitify_first(tmp_path):
    order = []
    sync = MagicMock()

    def _create(payload):
        order.append(("remote", payload["name"]))
        return {"id": "remote-123"}

    sync.client.create_habit.side_effect = _create
    h = _handlers_with_store(tmp_path)
    h.habitify_sync = sync

    added = await h.add_habit_from_text("Cold shower")

    assert added == "Cold shower"
    habit = h.store.list_habits(tracked_only=False)[0]
    assert habit["name"] == "Cold shower"
    assert habit["habitify_id"] == "remote-123"
    # created remotely, and the local row already carries the returned id —
    # never a moment where a local-only row exists.
    assert order == [("remote", "Cold shower")]

    payload = sync.client.create_habit.call_args.args[0]
    assert payload["occurrence"] == {"type": "weekDays", "days": [0, 1, 2, 3, 4, 5, 6]}


@pytest.mark.asyncio
async def test_add_habit_from_text_translates_day_tag_to_habitify_weekdays(tmp_path):
    sync = MagicMock()
    sync.client.create_habit.return_value = {"id": "remote-456"}
    h = _handlers_with_store(tmp_path)
    h.habitify_sync = sync

    await h.add_habit_from_text("Stretch [mon,wed,fri]")

    payload = sync.client.create_habit.call_args.args[0]
    # Local Monday=0 [0, 2, 4] -> Habitify Sunday=0 [1, 3, 5].
    assert payload["occurrence"]["days"] == [1, 3, 5]


@pytest.mark.asyncio
async def test_add_habit_from_text_habitify_failure_creates_nothing_locally(tmp_path):
    sync = MagicMock()
    sync.client.create_habit.side_effect = HabitifyError(503, "unavailable")
    h = _handlers_with_store(tmp_path)
    h.habitify_sync = sync

    with pytest.raises(HabitifyError):
        await h.add_habit_from_text("Cold shower")

    assert h.store.list_habits(tracked_only=False) == []


# --- sync_from_habitify's untrack loop must leave alone habits that were
# never synced to Habitify (habitify_id == "") — only reconcile habits that
# were previously Habitify-managed and have since disappeared remotely. ---


def test_sync_from_habitify_does_not_untrack_a_never_synced_habit(tmp_path):
    logs = Logs(str(tmp_path))
    context = Context(tmp_path)
    store = HabitStore(logs.db, context)
    store.add("Purely local habit")  # habitify_id == "" — created before this fix,
    # or intentionally never pushed

    store.sync_from_habitify([])  # nothing in Habitify at all

    habit = store.list_habits(tracked_only=False)[0]
    assert habit["tracked"] is True


def test_sync_from_habitify_untracks_a_habit_removed_remotely(tmp_path):
    logs = Logs(str(tmp_path))
    context = Context(tmp_path)
    store = HabitStore(logs.db, context)
    store.add("Was in Habitify", habitify_id="remote-789")

    store.sync_from_habitify([])  # no longer present in Habitify's response

    habit = store.list_habits(tracked_only=False)[0]
    assert habit["tracked"] is False

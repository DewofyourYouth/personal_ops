from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

from habit_handlers import HabitHandlers
from habitify import HabitifyError


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

"""The `slept …`/`sleep: …` intake must log a `sleep` metric only when an explicit number
is present — otherwise "slept badly" would be hijacked out of being a normal checkin."""

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import text_router
from text_router import TextRouter


def _make_router():
    services = types.SimpleNamespace(
        logs=MagicMock(),
        agenda=MagicMock(),
        queue=MagicMock(),
        backlog=MagicMock(),
        reminders=MagicMock(),
        gcal=MagicMock(),
        planner=MagicMock(),
        hypotheses=MagicMock(),
    )
    return TextRouter(
        bot=AsyncMock(), services=services, shabbat=MagicMock(), allowed_user=123
    )


@pytest.mark.asyncio
async def test_slept_with_hours_logs_sleep_metric():
    router = _make_router()
    await router.process_text("slept 7 hours", AsyncMock())
    router.logs.write_metric.assert_called_once_with("sleep", 7.0, "h")


@pytest.mark.asyncio
async def test_sleep_colon_decimal_logs_metric():
    router = _make_router()
    await router.process_text("sleep: 6.5", AsyncMock())
    router.logs.write_metric.assert_called_once_with("sleep", 6.5, "h")


@pytest.mark.asyncio
async def test_slept_badly_without_number_is_not_a_sleep_metric(monkeypatch):
    # No number → falls through to normal classification, not the sleep metric.
    monkeypatch.setattr(
        text_router, "classify_entry", AsyncMock(return_value="checkin")
    )
    router = _make_router()
    await router.process_text("slept badly, feeling groggy", AsyncMock())
    router.logs.write_metric.assert_not_called()

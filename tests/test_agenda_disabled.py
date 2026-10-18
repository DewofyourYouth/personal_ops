"""Opt-out must survive a persistent-job-store restart without touching user data."""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from agenda_handlers import AgendaHandlers
from config import Config
from scheduling import start
from status_handlers import StatusHandlers

_calls = []


async def scheduled_job():
    _calls.append("executed")


JOBS = dict.fromkeys(
    [
        "morning_plan",
        "remind_upcoming",
        "check_reminders",
        "check_hypotheses",
        "daily_digest",
        "weekly_digest",
        "weekly_mine",
        "weekly_retrain",
        "weekly_goal_review",
    ],
    scheduled_job,
)


@pytest.mark.asyncio
async def test_persisted_overdue_plan_removed_before_execution_and_can_be_restored(
    tmp_path,
):
    _calls.clear()
    marker = tmp_path / "2026-10-17-agenda.json"
    marker.write_text('["existing user data"]')
    old = AsyncIOScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path}/scheduler.db")
        }
    )
    old.start(paused=True)
    overdue = datetime.now(ZoneInfo("UTC")) - timedelta(seconds=1)
    old.add_job(scheduled_job, "date", run_date=overdue, id="morning_plan")
    old.add_job(scheduled_job, "interval", hours=12, id="custom_reminder")
    old.shutdown()
    await asyncio.sleep(0)

    for enabled in (False, False, True):
        scheduler = start(
            str(tmp_path),
            JOBS,
            agenda_generation_enabled=enabled,
            plan_hour=8,
            plan_minute=0,
            tz=ZoneInfo("Asia/Jerusalem"),
            extra_jobs=[
                {
                    "id": "weight_plugin",
                    "func": scheduled_job,
                    "trigger": "interval",
                    "kwargs": {"hours": 12},
                }
            ],
        )
        try:
            await asyncio.sleep(0)
            assert (scheduler.get_job("morning_plan") is not None) == enabled
            assert scheduler.get_job("custom_reminder") is not None
            assert scheduler.get_job("weight_plugin", jobstore="memory") is not None
            assert all(
                scheduler.get_job(name) for name in JOBS if name != "morning_plan"
            )
            assert marker.read_text() == '["existing user data"]'
            assert _calls == []  # no overdue agenda escaped during startup
        finally:
            scheduler.shutdown()
            await asyncio.sleep(0)


def feature(enabled=False):
    return AgendaHandlers(
        bot=AsyncMock(),
        agenda=MagicMock(),
        queue=MagicMock(),
        gcal=MagicMock(),
        planner=MagicMock(),
        logs=MagicMock(),
        allowed_user=1,
        generation_enabled=enabled,
    )


@pytest.mark.asyncio
async def test_disabled_proposal_is_silent_and_does_not_consume_queue_or_call_services():
    h = feature()
    await h.send_proposal(1)
    for service in (h.bot, h.agenda, h.queue, h.gcal, h.planner, h.logs):
        assert service.mock_calls == []


@pytest.mark.asyncio
async def test_disabled_manual_plan_and_stale_proposal_buttons_do_not_generate_or_commit():
    h = feature()
    query = SimpleNamespace(answer=AsyncMock(), data="pt_all")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        message=SimpleNamespace(reply_text=AsyncMock()),
        callback_query=query,
    )
    await h.cmd_plan(update, None)
    assert "disabled" in update.message.reply_text.call_args.args[0]
    h._pending[1] = {"items": ["old proposal"], "selected": {0}, "editing": 0}
    await h.handle_proposal_callback(update, None)
    assert not await h.try_handle_proposal_edit(update)
    assert h.agenda.mock_calls == []
    assert h.planner.mock_calls == []


def test_disabled_status_does_not_prompt_to_generate():
    h = feature()
    h.agenda.get_status.return_value = []
    status = StatusHandlers(None, h, None, None, None, 1)
    assert status._agenda_section() == ""
    assert status._rich_agenda_html() == ""


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("false", False), ("0", False), ("true", True), ("YES", True)],
)
def test_config_generation_switch(monkeypatch, value, expected):
    monkeypatch.setenv("OPS_BOT_TOKEN", "test")
    monkeypatch.setenv("OPS_CHAT_ID", "1")
    monkeypatch.delenv("OPS_AGENDA_GENERATION_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("OPS_AGENDA_GENERATION_ENABLED", value)
    assert Config.from_env().agenda_generation_enabled is expected


def test_invalid_switch_fails_loudly(monkeypatch):
    monkeypatch.setenv("OPS_BOT_TOKEN", "test")
    monkeypatch.setenv("OPS_CHAT_ID", "1")
    monkeypatch.setenv("OPS_AGENDA_GENERATION_ENABLED", "ture")
    with pytest.raises(ValueError, match="OPS_AGENDA_GENERATION_ENABLED"):
        Config.from_env()


@pytest.mark.asyncio
async def test_stale_scheduled_entrypoint_is_silent_when_disabled():
    # Import only this thin job: importing bot.py would open production services.
    import ast

    tree = ast.parse((Path(__file__).parent.parent / "ops" / "bot.py").read_text())
    job = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "morning_plan"
    )
    namespace = {
        "config": SimpleNamespace(agenda_generation_enabled=False),
        "_bot": AsyncMock(),
        "agenda_feature": AsyncMock(),
        "shabbat_": MagicMock(),
    }
    exec(compile(ast.Module(body=[job], type_ignores=[]), "bot.py", "exec"), namespace)  # noqa: S102 -- trusted local source, isolated from startup side effects
    await namespace["morning_plan"]()
    assert namespace["agenda_feature"].mock_calls == []
    assert namespace["_bot"].mock_calls == []


def test_logging_keyboard_omits_agenda_but_preserves_backlog_and_edit_actions():
    from text_router import TextRouter

    router = TextRouter.__new__(TextRouter)
    router.agenda_feature = feature()
    router.reclassify = SimpleNamespace(confidence_threshold=0.55)
    keyboard = router._entry_keyboard(1, "task", 1.0)
    callbacks = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert "route:agenda:1" not in callbacks
    assert "route:backlog:1" in callbacks
    assert any(data.startswith("rc:") for data in callbacks)

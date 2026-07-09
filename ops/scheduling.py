"""Scheduling layer — owns the APScheduler instance, the recurring-job
schedule, and start/stop. Keeps apscheduler and the cron specs out of the
Telegram entrypoint.

The job *functions* still live in bot.py for now (they call its handler/UI
helpers); they're passed in here as a name→coroutine map. They'll move here
once those helpers are extracted.
"""

from zoneinfo import ZoneInfo

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TZ = ZoneInfo("Asia/Jerusalem")


def start(
    log_dir: str, jobs: dict, *, plan_hour: int, plan_minute: int, extra_jobs: list = ()
) -> AsyncIOScheduler:
    """Build the scheduler (SQLite job store so jobs survive restarts), register
    the recurring jobs, start it, and return the running instance.

    jobs: name → coroutine function for each core scheduled task.
    extra_jobs: per-plugin specs ``{"id", "func", "trigger", "kwargs"}``.
    """
    scheduler = AsyncIOScheduler(
        jobstores={
            # Core jobs are module-level functions → picklable, so they persist.
            "default": SQLAlchemyJobStore(url=f"sqlite:///{log_dir}/scheduler.db"),
            # Plugin jobs are bound methods holding the (unpicklable) Bot; keep them
            # in memory and re-register them on each boot.
            "memory": MemoryJobStore(),
        },
        timezone=TZ,
    )
    scheduler.add_job(
        jobs["morning_plan"],
        "cron",
        hour=plan_hour,
        minute=plan_minute,
        id="morning_plan",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["remind_upcoming"],
        "interval",
        seconds=600,
        id="remind_upcoming",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["check_reminders"],
        "interval",
        seconds=60,
        id="check_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["check_hypotheses"],
        "cron",
        hour=10,
        minute=0,
        id="check_hypotheses",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["daily_digest"],
        "cron",
        hour=22,
        minute=30,
        id="daily_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["weekly_digest"],
        "cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        id="weekly_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["weekly_mine"],
        "cron",
        day_of_week="sun",
        hour=21,
        minute=0,
        id="weekly_mine",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["weekly_retrain"],
        "cron",
        day_of_week="sun",
        hour=21,
        minute=30,
        id="weekly_retrain",
        replace_existing=True,
    )
    for spec in extra_jobs:
        scheduler.add_job(
            spec["func"],
            spec["trigger"],
            id=spec["id"],
            jobstore="memory",
            replace_existing=True,
            **spec.get("kwargs", {}),
        )
    scheduler.start()
    return scheduler


def shutdown(scheduler: AsyncIOScheduler | None) -> None:
    # Guard: if startup failed before the scheduler started, shutdown() raises
    # and masks the real error, turning a transient hiccup into a crash loop.
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)

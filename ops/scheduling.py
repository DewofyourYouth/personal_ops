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
from apscheduler.triggers.cron import CronTrigger


def start(
    log_dir: str,
    jobs: dict,
    *,
    plan_hour: int,
    plan_minute: int,
    extra_jobs: list = (),
    tz: ZoneInfo,
) -> AsyncIOScheduler:
    """Build the scheduler (SQLite job store so jobs survive restarts), register
    the recurring jobs, start it, and return the running instance.

    jobs: name → coroutine function for each core scheduled task.
    extra_jobs: per-plugin specs ``{"id", "func", "trigger", "kwargs"}`` — some
    are cron jobs too (habit checks, check-ins).
    tz: the active location's timezone at startup (see location.py). Every
    cron job registered here (core or plugin) picks up the scheduler's default
    tz; if the active location later changes, call reschedule_cron_jobs() to
    move them all to the new one.
    """
    scheduler = AsyncIOScheduler(
        jobstores={
            # Core jobs are module-level functions → picklable, so they persist.
            "default": SQLAlchemyJobStore(url=f"sqlite:///{log_dir}/scheduler.db"),
            # Plugin jobs are bound methods holding the (unpicklable) Bot; keep them
            # in memory and re-register them on each boot.
            "memory": MemoryJobStore(),
        },
        timezone=tz,
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


def reschedule_cron_jobs(scheduler: AsyncIOScheduler, tz: ZoneInfo) -> None:
    """Move every cron-triggered job (core and plugin-contributed alike) onto
    tz, keeping its existing schedule fields (hour, minute, day_of_week, ...).
    Called when the active location changes (see location.add_listener in
    bot.py) — a bare variable swap wouldn't move already-registered jobs,
    since APScheduler bakes the timezone into each CronTrigger at add_job
    time, but every job here is already registered with replace_existing=True,
    so re-registering with a new tz is a plain, safe update, no teardown."""
    for job in scheduler.get_jobs():
        if not isinstance(job.trigger, CronTrigger):
            continue  # interval jobs are tz-independent, nothing to move
        field_values = {
            f.name: ",".join(str(e) for e in f.expressions) for f in job.trigger.fields
        }
        scheduler.reschedule_job(
            job.id, trigger=CronTrigger(timezone=tz, **field_values)
        )


def shutdown(scheduler: AsyncIOScheduler | None) -> None:
    # Guard: if startup failed before the scheduler started, shutdown() raises
    # and masks the real error, turning a transient hiccup into a crash loop.
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)

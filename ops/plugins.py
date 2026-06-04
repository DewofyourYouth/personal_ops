"""Plugin registry — the one place tracking-domain plugins are listed.

A plugin is a feature class (same shape as the core handlers: built with the bot
+ the services it needs, handlers as methods, self-registers via `register(app)`).
Listing it here is what makes it active; removing it turns the domain off without
touching the entry point.

`bot.py` calls `build_plugins(...)`, then loops the result to register each and
collect any scheduled jobs.
"""
from types import SimpleNamespace


def build_plugins(bot, services: SimpleNamespace) -> list:
    """Construct the active plugins. Empty for now — habits/food/etc. land here
    one at a time. `services` carries the shared domain singletons."""
    return [
        # HabitHandlers(bot, services.logs, services.context),
    ]


def collect_jobs(plugins: list) -> dict:
    """Gather scheduled jobs each plugin exposes via an optional `jobs` dict
    (id -> coroutine). Plugins without scheduled work contribute nothing."""
    jobs: dict = {}
    for plugin in plugins:
        jobs.update(getattr(plugin, "jobs", {}))
    return jobs

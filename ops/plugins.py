"""Plugin registry — the one place tracking-domain plugins are listed.

A plugin is a feature class (same shape as the core handlers: built with the bot
+ the services it needs, handlers as methods, self-registers via `register(app)`).
Listing it here is what makes it active; removing it turns the domain off without
touching the entry point.

`bot.py` calls `build_plugins(...)`, then loops the result to register each and
collect any scheduled jobs.
"""

from types import SimpleNamespace

from food_handlers import FoodHandlers
from grocery import GroceryHandlers
from habit_handlers import HabitHandlers
from routines import RoutineHandlers


def build_plugins(bot, services: SimpleNamespace) -> list:
    """Construct the active plugins. `services` carries the shared domain
    singletons; a domain is active iff it's in this list."""
    return [
        HabitHandlers(
            bot,
            services.logs,
            services.context,
            services.allowed_user,
            services.planner,
            quiet_window=getattr(services, "quiet_window", None),
        ),
        FoodHandlers(bot, services.logs, services.allowed_user),
        RoutineHandlers(bot, services.logs, services.context, services.allowed_user),
        GroceryHandlers(bot, services.logs, services.allowed_user),
    ]


def collect_jobs(plugins: list) -> list:
    """Gather the scheduled-job specs each plugin exposes via an optional `jobs`
    list — each a dict ``{"id", "func", "trigger", "kwargs"}``. Plugins without
    scheduled work contribute nothing."""
    specs: list = []
    for plugin in plugins:
        specs.extend(getattr(plugin, "jobs", []))
    return specs

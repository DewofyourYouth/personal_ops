# Plugin Architecture — Spec

Status: **in progress.** Supersedes the idea-capture in `PLUGIN_ARCHITECTURE.md`.
Authored 2026-06-03; revised 2026-06-04 to match the layering actually being built and the
capability-protocol decision.

Goal: make a tracking domain (habits, food, metrics, …) a **single self-contained feature
class** that registers its own commands, callbacks, and jobs, and exposes its data through
small shared protocols — so adding or removing a domain touches its own file, not the core.
Incremental: the core grows a registry, domains move in one at a time. No big-bang rewrite.

## How this revision differs from the first draft

The first draft proposed one fat `Module` Protocol with ~8 fields and a `Core` object whose
`setup(core)` handed dependencies in. After building the first feature class (`AgendaHandlers`)
we changed two things:

- **Dependency injection via the constructor**, not `Core.setup()`. A feature class is built
  `Feature(bot, *services)` directly — plain, type-hinted, testable. No `Core` god-object.
- **Small capability protocols**, not one fat interface. A plugin implements only the narrow
  protocols for what it actually shares (e.g. `Trackable`), satisfied **structurally**.

## The shape of a feature (what we built)

The reference is `ops/agenda_handlers.py`. A feature is a class:

```python
class AgendaHandlers:
    def __init__(self, bot: Bot, agenda: Agenda, queue: AgendaQueue, ...) -> None:
        self.bot = bot
        self.agenda = agenda
        self._pending: dict = {}            # conversation state — instance, not a global

    async def cmd_plan(self, update, ctx): ...        # handlers are methods
    async def handle_proposal_callback(self, update, ctx): ...

    def register(self, app: Application) -> None:     # self-wires its surface
        app.add_handler(CommandHandler("plan", self.cmd_plan))
        app.add_handler(CallbackQueryHandler(self.handle_proposal_callback, pattern="^pt_"))
```

- Constructed with the **bot + the domain services it needs** (DI by constructor).
- **Handlers are methods**; conversation state lives on the instance (no module globals).
- **`register(app)`** wires its own commands + callbacks.
- **Scheduled jobs are bound methods** the entry point hands to `scheduling.start(...)`.
- Imports only leaf modules (`tg_common`, domain services) — never `bot.py`, so no cycles.

## Core vs. plugin

Same class shape; the difference is **how it's wired**.

- **Core features** are wired *directly* in `main()` (constructed, `register(app)` called by hand).
  These are load-bearing and always present: the message **dispatcher**, **agenda** (the
  proposal/advisory flow — the heart), **reminders**.
- **Plugins** are the **tracking domains** — **habits, food, metrics, backlog, values, context**
  — registered through a **registry** (a `PLUGINS` list the entry point iterates). Listing is
  what makes them optional; a domain absent from the list contributes nothing.

The registry is **structure first** — clean, self-contained, capability-discoverable modules.
Enable/disable per deployment (drop from the list, or an `OPS_PLUGINS` env) is a cheap bonus it
enables, not a feature that must work day one.

## The registry

```python
# ops/plugins.py — the one place plugins are listed
def build_plugins(bot, services) -> list:
    return [
        HabitHandlers(bot, services.logs, services.context),
        FoodHandlers(bot, services.logs),
        MetricsHandlers(bot, services.logs),
        # ...
    ]
```

```python
# in main(), after build():
plugins = build_plugins(app.bot, services)
for p in plugins:
    p.register(app)
jobs = {**core_jobs, **collect_jobs(plugins)}   # bound methods
scheduling.start(LOG_DIR, jobs, ...)
```

The entry point loops the list — it doesn't name each plugin. Adding a domain = one line in
`build_plugins`, nothing in `main()`.

## Capability protocols (core, small, structural)

Be explicit about *only* the genuinely shared things, split by capability. `@runtime_checkable`
so the registry/digest/eval can **discover** which plugins have a capability without a hardcoded
list.

```python
# ops/capabilities.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class Trackable(Protocol):
    """A domain that can summarize its data over a window — for the digest and
    the eval harness."""
    def summary(self, days: int) -> str: ...
```

- Plugins satisfy these **structurally** — `HabitHandlers` just has a `summary(days)` method;
  it does **not** inherit `Trackable`. No hierarchy, no coupling.
- Domain-specific behavior (habit streaks, food macros) stays **off** the protocol, on the class.
- Add a second protocol (e.g. `Recordable` with `record(...)`) only when a second real shared
  need appears — **interface segregation over one fat interface**. If we're cramming
  domain-only methods onto a protocol, that's the signal it's the wrong abstraction.

Consumers iterate by capability:

```python
trackers = [p for p in plugins if isinstance(p, Trackable)]
digest_sections = [t.summary(days) for t in trackers]
```

## Training (eval harness) consumes tracking

**Training = the eval harness** ([EVAL_LOOP_SPEC.md](EVAL_LOOP_SPEC.md)) — it trains/tunes
personal_ops's advice. It is **core**, and it **consumes** `Trackable` plugins one-directionally
(behavioral traces, outcomes, metric movement are its signals). The arrow points one way —
Training reads trackers; trackers know nothing about Training — so there is no cycle.

## Shared services & cross-plugin state

- **DI by constructor.** Plugins receive `bot` + the domain services they need (`logs`,
  `context`, `planner`, …). The LLM edge lives in `llm.py`; `planner` is its generation half;
  both are shared services, consistent with "AI at the edges, deterministic core."
- **Plugins don't import each other.** Shared signal flows through the common `ops.db`: food
  writes entries/metrics; another domain reads them via `logs`/`db`. If a plugin needs another's
  data, it reads the table, not the Python object.

## Already extracted (the layers this builds on)

- `ops/llm.py` — all `anthropic`/`openai` (input interpretation + generation half in `planner.py`).
- `ops/scheduling.py` — the APScheduler instance + recurring-job schedule.
- `ops/tg_common.py` — shared Telegram UI helpers (`safe_answer`, `encourage`).
- `ops/bot_constants.py` — prefixes, icons, copy.
- `ops/agenda_handlers.py` — the reference **core** feature class.

## Build order

1. **`capabilities.py`** — the `Trackable` protocol (and only that, for now).
2. **`plugins.py` registry** — `build_plugins(...)` + the `main()` loop + job collection.
   With an empty list, the bot behaves exactly as today.
3. **Habits → first plugin** — extract `HabitHandlers` (commands, `hb_done:` callback,
   checklist); implement `summary(days)` to satisfy `Trackable`; register via the list.
4. **Food → second plugin** — same shape.
5. **Metrics, backlog, values, context** — opportunistically, as each needs work.
6. **Digest + eval** consume `[p for p in plugins if isinstance(p, Trackable)]`.

## Deliberately deferred

- **Per-plugin DB schema/migrations.** All tables stay in the shared `ops.db` schema in `db.py`.
- **The `_process_text` NL router decomposition.** Prefix/natural-language routing stays central
  for now; per-plugin `try_handle(text)` matchers come later (the `AgendaHandlers
  .try_handle_proposal_edit` delegation is the first taste of that pattern).
- **Untrusted/third-party plugins & hot reload.** Single-user, trusted, in-repo list; restart to
  change (cheap, containerized).

## Relationship to other specs

- [DASHBOARD_API_SPEC.md](DASHBOARD_API_SPEC.md): a plugin may later expose an API router for its
  ingest/read routes; same capability-discovery idea.
- [EVAL_LOOP_SPEC.md](EVAL_LOOP_SPEC.md): Training consumes `Trackable` plugins.
- [VPS_MIGRATION.md](VPS_MIGRATION.md): the public-repo goal benefits — plugins become the unit
  others enable/customize.

## Open questions

1. Where do the `_awaiting_*` interactive flows end up — per-plugin instance state (as
   `AgendaHandlers._pending` now is) reached via a `try_handle(update)` method the dispatcher
   calls, or a small shared state module? Leaning per-plugin, dispatcher delegates.
2. Does `summary(days)` return a string (simple, concatenated into the digest prompt) or a
   richer structured object the LLM weighs? Start with a string.

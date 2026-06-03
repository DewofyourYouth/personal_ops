# Plugin Architecture — Spec

Status: **proposed**. Supersedes the idea-capture in `PLUGIN_ARCHITECTURE.md`.
Authored 2026-06-03.

Goal: make a tracking domain (health, food, habits, jobs, sleep, reading, …) a **single
self-contained module** that registers its own commands, parsing, scheduled jobs, digest
contribution, context files, and API routes — so adding one as needs change touches the
module's own folder, not the core. Incremental: the core grows a registry, modules move in
one at a time. No big-bang rewrite.

## Current reality (what a module is scattered across today)

A domain like habits currently lives in several places in the monolith:

| Capability | Where it lives now |
|---|---|
| Service/state | `habit_tracker.py`, constructed `Baseline(LOG_DIR)` etc. at `bot.py:67-75` |
| Commands | hand-registered `app.add_handler(CommandHandler("habits", cmd_habits))` in `main()` |
| Button callbacks | `CallbackQueryHandler(handle_habit_callback, pattern="^hb_done:")` in `main()` |
| Prefix → tag | `PREFIXES` dict (`bot.py:76+`) |
| Natural-language input | a regex branch inside `_process_text` |
| Scheduled jobs | `_scheduler.add_job(...)` in `_post_init` |
| Digest contribution | `planner_` pulls logs/metrics when building the digest |
| Context files | loaded via `Context` |
| API write/read | (planned) a route in the dashboard FastAPI app — see [DASHBOARD_API_SPEC.md](DASHBOARD_API_SPEC.md) |

Adding a domain means editing all of these. The seams are visible; this spec names them.

## The module contract

A module is a folder under `modules/` exposing one object that implements this protocol.
Every field is optional except `name` — a module uses only the seams it needs.

```python
# core/plugin.py
from typing import Protocol, Callable, Awaitable, runtime_checkable

@runtime_checkable
class Module(Protocol):
    name: str                       # unique slug, e.g. "habits"
    enabled: bool                   # default True; gate per deployment

    def setup(self, core: "Core") -> None: ...
        # construct state/services from shared deps (core.log_dir, core.db, core.logs,
        # core.context, core.planner, core.scheduler, core.bot). Called once at startup.

    # --- Telegram wiring (core registers these; bot.py stops growing) ---
    commands: list[tuple[str, Handler]]        # [("habits", cmd_habits), ("h", cmd_habits)]
    callbacks: list[tuple[str, Handler]]       # [("^hb_done:", handle_habit_callback)]
    prefixes: dict[str, str]                   # {"food:": "#food"}
    text_matchers: list["TextMatcher"]         # ordered NL handlers (the _process_text branches)

    # --- Background work ---
    jobs: list["JobSpec"]                      # [JobSpec(check_habits, "interval", seconds=3600)]

    # --- Reflective output ---
    def digest_context(self, days: int) -> str | None: ...   # section injected into the digest prompt
    context_files: list[str]                                  # files to load into LLM context

    # --- HTTP (dashboard API) ---
    def api_router(self) -> "APIRouter | None": ...           # module's /metrics, /jobs, GET reads
```

Supporting types:

```python
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

@dataclass
class TextMatcher:
    pattern: re.Pattern                  # matched against the cleaned message text
    handle: Callable[[re.Match, Reply, int], Awaitable[bool]]
    priority: int = 100                  # lower runs first; core sorts globally

@dataclass
class JobSpec:
    func: Callable[[], Awaitable[None]]
    trigger: str                         # "cron" | "interval" | "date"
    id: str | None = None
    kwargs: dict = field(default_factory=dict)   # passed to scheduler.add_job
```

`Reply` is the existing `update.message.reply_text`-style callable already threaded through
`_process_text`, so matchers move over verbatim.

## Core: the registry/loader

```python
# core/registry.py
class Core:
    def __init__(self, log_dir, db, logs, context, planner, scheduler, bot): ...
    modules: list[Module]

    def load(self, modules: list[Module]):
        for m in modules:
            if m.enabled:
                m.setup(self)
                self.modules.append(m)

    def wire_telegram(self, app):
        for m in self.modules:
            for name, h in m.commands:   app.add_handler(CommandHandler(name, h))
            for pat, h in m.callbacks:   app.add_handler(CallbackQueryHandler(h, pattern=pat))
        # PREFIXES and text_matchers are merged into the single dispatcher below.

    def wire_jobs(self, scheduler):
        for m in self.modules:
            for j in m.jobs:
                scheduler.add_job(j.func, j.trigger, id=j.id or f"{m.name}:{j.func.__name__}",
                                  replace_existing=True, **j.kwargs)

    def merged_prefixes(self) -> dict[str, str]: ...
    def sorted_text_matchers(self) -> list[TextMatcher]: ...
    def digest_sections(self, days) -> list[str]: ...   # planner concatenates these
    def api_routers(self): ...                          # FastAPI app includes each
```

The generic message dispatcher replaces the long `if/elif` chain in `_process_text`:
clean text → check prefixes → run `sorted_text_matchers` in priority order until one returns
`True` (handled) → else fall through to the default log/checkin behavior. The interactive
`_awaiting_*` flows stay in core for now (cross-cutting state), or each becomes module-owned
later (see open questions).

## Module enable/disable (config)

```python
# modules/__init__.py  — the one place that lists active modules
from modules.habits import HabitsModule
from modules.jobs import JobsModule
ACTIVE = [HabitsModule(), JobsModule(), HealthModule(), FoodModule()]
```

Or env-driven (`OPS_MODULES=habits,jobs,health`) so personal vs. shared/public deployments
differ without code changes. A module absent from the list contributes nothing — no command,
no job, no route.

## Shared services (dependency injection, not imports)

Modules receive what they need from `core` in `setup()` rather than importing globals:
`core.log_dir`, `core.db` (the one `ops.db`), `core.logs`, `core.context`, `core.planner`
(the LLM — kept a **shared core service**, consistent with "AI at the edges, deterministic
core"), `core.scheduler`, `core.bot`. This keeps modules decoupled and testable.

## Cross-module state (the food → health question)

Modules **do not import each other.** Shared signal flows through the common store: food
writes metrics/entries to `ops.db`; the health module reads them back via `core.db` /
`core.logs`. If a module needs another's data, it reads the table, not the Python object.
For richer needs, a module may expose a small read method other modules call through a
typed registry lookup (`core.module("food").recent_calories(days)`), but default to the DB.

## Incremental migration (strangler, not rewrite)

1. **Add the seams, change nothing else.** Introduce `core/plugin.py` + `core/registry.py`.
   `bot.py` builds a `Core`, registers **zero** modules, still works exactly as today.
2. **Pilot one module.** Extract **habits** (self-contained, already in `habit_tracker.py`)
   into `modules/habits/`. Move its command, `hb_done:` callback, any job, and digest pull.
   Delete those lines from `bot.py`. Verify parity.
3. **Extract opportunistically.** Each time a domain needs work (or a new one is added),
   move/author it as a module. `jobs` is a natural next one — it already owns its table and
   gets the `POST /jobs` route, so `api_router()` lands there.
4. **Stop when it stops hurting.** Core-only concerns (auth gate, `_awaiting_*` flows,
   unicode cleanup, the default log path) can stay in core indefinitely.

## Worked example — a brand-new module

Adding a "reading" tracker later is one folder, no core edits beyond the `ACTIVE` list:

```python
# modules/reading/__init__.py
class ReadingModule:
    name = "reading"; enabled = True
    prefixes = {"read:": "#reading"}

    def setup(self, core):
        self.logs = core.logs
        self.commands = [("reading", self.cmd_reading), ("rd", self.cmd_reading)]
        self.jobs = [JobSpec(self.nightly_nudge, "cron", kwargs=dict(hour=21))]

    async def cmd_reading(self, update, ctx):
        await update.message.reply_text(self._summary())

    def digest_context(self, days):
        pages = self.logs.db.metric_sum("pages", days)
        return f"Reading: {pages} pages over {days}d." if pages else None

    def api_router(self):
        r = APIRouter(prefix="/reading")
        @r.post("")  # POST /reading {"pages": 32}
        def log_pages(body: PagesIn, _=Depends(auth)):
            self.logs.write_metric("pages", body.pages)
            return {"logged": body.pages}
        return r
```

## Deliberately deferred

- **Per-module migrations/schema.** For now all tables live in the shared `ops.db` schema
  in `db.py`; a module-owned migration system is out of scope until module count justifies it.
- **Third-party/untrusted plugins.** This is a single-user, trusted, in-repo registry —
  no sandboxing, no dynamic plugin discovery from disk. `ACTIVE` is an explicit Python list.
- **Hot reload.** Modules load at startup; changing them means a restart (cheap, containerized).

## Relationship to other specs

- [DASHBOARD_API_SPEC.md](DASHBOARD_API_SPEC.md): each module's `api_router()` is how
  `/metrics`, `/jobs`, and later read routes attach to the FastAPI app per-module.
- [VPS_MIGRATION.md](VPS_MIGRATION.md): revisit "after VPS stable + API v1 exists." The
  public-repo goal benefits directly — modules become the unit others enable/customize.
- Agenda paradigm work stays a core concern (it shapes how *all* modules surface advice),
  not a module.

## Open questions

1. Do the `_awaiting_*` interactive flows become module-owned (each module gets a slice of
   conversation state), or stay a core dispatcher concern? Lean core-owned until painful.
2. Digest assembly: planner concatenates `digest_context()` sections (simple), or modules
   get a richer structured contribution the LLM weighs? Start with concatenation.
3. Module ordering for `text_matchers` — global priority int (proposed) vs. per-module
   ordering with core deciding module order. Global int is simplest.

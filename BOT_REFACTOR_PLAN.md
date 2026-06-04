# bot.py Extraction Plan

Status: **proposed** (not executed). Authored 2026-06-04.
The concrete, mechanical bridge from today's 2,033-line `ops/bot.py` toward
[PLUGIN_ARCHITECTURE_SPEC.md](PLUGIN_ARCHITECTURE_SPEC.md). This plan **does not** adopt the
full Module/registry protocol yet — it splits by concern into flat modules behind a
`register(app)` seam, which is the precursor the plugin system later formalizes.

## Why bot.py is hard to split (the real coupling)

It's not the length — it's **shared module-global state** every handler reaches into directly:

- **Service singletons** (constructed at bot.py:67–75): `logs, agenda_, queue_, backlog_,
  reminders, gcal_, context_, planner_, baseline_`.
- **Runtime singletons**: `_scheduler` (bot.py:61), `_bot` (set in `_post_init`).
- **Config consts**: `TOKEN, ALLOWED_USER, MODEL, PLAN_HOUR, PLAN_MINUTE, LOG_DIR, PREFIXES`.
- **Conversation state**: `_pending, _awaiting_time, _awaiting_context, _awaiting_candles,
  _awaiting_voice_edit, _awaiting_reminder_edit` (and `_awaiting_job`, deleted with jobs).

Move a handler to its own file and it loses these. So the *first* work is giving them a home.

## Step 0 — leaf modules for shared state (no handlers move yet)

Create three dependency-free "leaf" modules. Everything imports *from* these; they import
nothing back. That keeps the dependency graph a DAG — **no import cycles.**

- **`ops/config.py`** — all env/config consts + `TZ`, `PREFIXES`, quiet-window consts.
- **`ops/services.py`** — constructs and holds the service singletons (imports `config`). Holds a
  mutable `bot` slot set at startup and the `scheduler` instance.
- **`ops/state.py`** — the conversation-state dicts, nothing else.

Then make bot.py import from these instead of defining them. **This step alone changes no
behavior and can land before anything else** (including before the VPS migration, safely).

Import discipline: keep new modules **flat in `ops/`** (e.g. `ops/h_agenda.py`), matching the
current flat-import style (`from agenda import Agenda`). A `handlers/` subpackage is cleaner but
needs packaging/`sys.path` changes — defer that to the plugin-system step.

## The seam: `register(app)` per module + `register_jobs(scheduler)`

Each extracted handler module exposes:

```python
def register(app):            # wires its own commands + callbacks
    app.add_handler(CommandHandler("habits", cmd_habits))
    app.add_handler(CallbackQueryHandler(handle_habit_callback, pattern="^hb_done:"))
```

bot.py's `main()` becomes a list of `module.register(app)` calls. Scheduled jobs get the same
treatment in a central **`ops/scheduling.py`** with `register_jobs(scheduler)` importing each
domain's job fn. These two seams are exactly what the plugin registry later automates.

## Extraction order (low → high coupling, one concern per commit)

1. **`ops/shabbat.py`** — `_shabbat_quiet_now, _in_active_window`, candle fns + quiet consts.
   A shared util many modules call; extracting it first unblocks the rest. Low coupling.
2. **`ops/common.py`** — `_safe_answer, _encourage, _digest_to_html`, and text utils
   `_normalize, _parse_time`. Pure helpers.
3. **`ops/h_digests.py`** — `cmd_digest, cmd_daily_digest, _digest_*` + jobs
   `scheduled_daily_digest, weekly_digest`. Self-contained (planner_, baseline_, bot, shabbat).
4. **`ops/h_reminders.py`** — `cmd_reminders, handle_reminder_*, _reminder*, _reminded_*` + the
   `check_reminders` job. Uses `reminders` service + `_awaiting_time/_awaiting_reminder_edit`.
5. **`ops/h_habits.py`** — `cmd_habits, cmd_habit_log, handle_habit_callback, _habits_message,
   _resolve_logged_to_habit`. (Natural home for the future EOD habit check-in.)
6. **`ops/h_metrics.py`** — `cmd_metrics` + mood/energy check-in (`_mood_energy_keyboard,
   handle_mood_energy_callback`).
7. **`ops/h_backlog.py`** — backlog + queue (`cmd_backlog, cmd_queue, handle_backlog_callback,
   _backlog_keyboard, _parse_queue_*`).
8. **`ops/h_agenda.py`** — the biggest: `cmd_plan, cmd_agenda*, handle_agenda_callback,
   handle_proposal_callback, _proposal_*, _commit_*, _send_proposal` + the `morning_plan` job.
   Uses `_pending`, agenda_, queue_, gcal_, planner_, shabbat.
9. **`ops/h_misc.py`** — `cmd_food, cmd_context, handle_context_callback, cmd_values, cmd_logs,
   cmd_help`; **`ops/h_voice.py`** — voice handlers; **calendar** `remind_upcoming, cmd_events`.
10. **Delete jobs** — `cmd_jobs, handle_job_callback, _job_*, _parse_job_from_text`, the
    `_awaiting_job` flow. Pure deletion (retired). Do this early too — it's free reduction.

## The hard part: `_process_text` (the ~300-line NL router)

`_process_text` (bot.py:539) is the central natural-language dispatcher and touches every domain.
Decompose it incrementally: each domain module gains a
`try_handle(text, reply, chat_id) -> bool` that returns True if it consumed the message. A thin
**`ops/dispatch.py`** runs them in order until one returns True, else falls through to the default
log. This is the lightweight precursor to the plugin spec's `text_matchers` and is the last,
most careful extraction — do it after the command/callback splits are stable.

Likewise `handle_message`'s `_awaiting_*` interceptor chain stays central in `dispatch.py` for now
(it's cross-cutting conversation state); per-module ownership comes with the plugin system.

## Target shape

- `bot.py` → ~250–350 lines: build `Application`, call each `module.register(app)`,
  `scheduling.register_jobs(scheduler)`, `_post_init/_post_shutdown`, `main()`.
- Each domain module: ~100–300 lines, owning its commands + callbacks + NL `try_handle` + jobs.
- Leaf modules (`config/services/state/shabbat/common`): small, dependency-free-ish.

## Discipline & safety

- **One concern per commit**, suite green after each (`pytest tests/`), `py_compile` + a smoke
  start of the bot. Behavior must be identical — this is a move, not a redesign.
- **No logic changes** mixed into an extraction commit. Bug fixes/redesigns go separately.
- After each step, the bot still runs from the same entrypoint (`python ops/bot.py`).

## Sequencing vs the VPS migration

Per the plugin spec's own guidance ("revisit after VPS stable"), the bulk extraction should land
**after** the migration, on stable ground — a big refactor right before a move adds risk to the
move. Two pieces are safe to do anytime, though, and shrink bot.py with ~zero risk:
**Step 0 (leaf modules)** and **Step 10 (delete retired jobs)**.

## Relationship to the plugin architecture

This plan produces flat modules + `register()`/`register_jobs()` seams. The plugin system then
formalizes those into the `Module` protocol + `Core` registry: `register(app)` → `commands`/
`callbacks` fields, `try_handle` → `text_matchers`, the leaf `services.py` → injected `Core`,
`register_jobs` → `jobs`. So nothing here is throwaway — it's the same decomposition, one
formalization step short of the end state.

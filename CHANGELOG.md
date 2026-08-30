# Changelog

Notable changes to the personal_ops bot, most recent first. Personal infrastructure — this
tracks what actually shipped, not a public release process.

## 2026-08-30

### Added

- **Import Habitify's own per-habit notes.** Habitify lets you attach a text or photo note when
  checking off a habit in the app; Personal Ops now imports new ones into `habit_notes`
  (`HabitifyClient.notes`, `HabitHandlers.sync_habitify_notes`) — the same table `/habitnote`
  writes to, so they show up in `/habitnote` history and the weekly habit-strategy prompt
  alongside notes added from Telegram. Import is idempotent on Habitify's note id, so re-scanning
  the lookback window can't double-import. (`ops/habitify.py`, `ops/habit_handlers.py`)

### Fixed

- **Notes sync was starving the Habitify completions sync.** The notes import above first
  shipped as its own 15-minute background job — one HTTP call per Habitify-managed habit, no
  bulk endpoint exists — on the same Habitify API key as the every-5-minute completions sync
  (`refresh_habits_from_habitify`). That sync silently swallows any `HabitifyError`, so
  contention or an error on the notes calls read as "nothing changed": habits checked off in
  Habitify stopped showing as done here. Dropped the standing job entirely — `sync_habitify_notes`
  now runs opportunistically from `cmd_habits` and `daily_habit_check`, right before each renders,
  so it fires only when a human is about to look at habit state instead of on a timer. Also
  hardened it so a malformed response for one habit can no longer abort the run for every habit
  after it. (`ops/habit_handlers.py`)

### Added

- **Habitify's explicit "failed" tap now records a miss immediately.** Previously an explicit
  fail in Habitify looked identical to "not logged yet" until the 22:45 auto-miss grace-cutoff
  inferred a miss from absence. `completions_for_date` now also surfaces daily habits Habitify
  reports as `status: failed`, and `refresh_habits_from_habitify` writes a `habit_missed` entry
  for any of them still unresolved locally — same-refresh as the completions pull, so it lands
  as soon as `/habits` or the nightly check runs. (`ops/habitify.py`, `ops/habit_handlers.py`)

### Fixed

- **`_pending_today_habits` used the wrong "today" near midnight.** Its default fell back to a
  naive `date.today()` while `logs.write()` buckets entries by `current_tz()`'s local day — for
  the few hours where the local zone has already crossed into a new day but the server's (UTC)
  clock hasn't, this read the previous day's (empty) entries and treated already-resolved habits
  as still pending. Found while adding the failed-tap sync above; affects the nightly check,
  `/status`, and auto-miss too. Now uses `current_tz()`-aware "today" like the rest of the
  Habitify sync path. (`ops/habit_handlers.py`)

## 2026-08-28

### Added

- **Time-bounded weekly focus goals.** State a goal for the week — via a `focus:` prefix or
  conversationally ("this week I want to focus on finishing the Haki debugging") — and it now
  shapes the daily agenda proposal while it's active, distinct from the static long-term
  `goals.md`. Reviewed (not silently expired) at a Sunday clearing session: one message per
  active goal with a Delete button and a Roll Over button. Ignored goals just stay active until
  explicitly cleared. (`ops/weekly_goals.py`, `ops/planner.py`, `ops/text_router.py`)
- **General intent-dispatch layer.** The bot now recognizes a small set of known actions —
  checking/setting candle lighting, creating a reminder, adding a calendar event — even when
  phrased indirectly ("if you don't have candle lighting time set, please set it") instead of as
  an exact command. Gated behind a cheap keyword pre-filter so a plain journal entry never pays
  for the extra LLM call. (`ops/text_router.py`, `ops/planner.py`)
- **Habitify integration (two-week trial).** New habits created in Personal Ops now also get
  created in Habitify; completions sync both directions. A migration script backfilled existing
  habit definitions and history. The bot keeps the conversational capture ("did Shacharit"), the
  streaks/schedule UI moves to Habitify. (`ops/habitify.py`, `scripts/migrate_habits_to_habitify.py`)
- **Location-aware timezone handling.** New `location.py` module replaces hardcoded timezone
  references across time tracking and weight logging with dynamic resolution based on the
  active location, including travel and Shabbat-location overrides.
- Checkin nudges now fire exactly twice a day (midday/evening) instead of a rolling
  "N hours since last checkin" schedule that could drift and over-fire.

### Fixed

- A weekly habit-suggestion review (set cue/days/rename/archive) could silently show a false
  "✅ done" message and mark itself accepted even when the underlying habit lookup failed
  (habit renamed/archived/removed since the suggestion was generated) — it now reports
  "Habit not found" and leaves the suggestion pending instead of lying about what happened.
  (`ops/habit_handlers.py`)
- `mine_logs.py` was conflating a genuine zero reading (0 steps, 0 hours slept) with a parse
  failure via truthiness checks (`if n:` instead of `if n is not None:`), silently dropping real
  zero-days from the correlations it computes.

### Changed

- Import ordering and formatting cleanup across several files; type hints and assertions
  tightened in `HabitHandlers`/`TextRouter`.

## 2026-08-27

### Added

- Agenda-item extraction now escalates to an LLM when the deterministic regex extraction looks
  suspect (multiple "agenda" mentions, a vague referent like "whatever I missed") instead of
  blindly trusting the regex — fixes a real bug where a rambling voice note got filed as a
  literal agenda item called "whatever I missed." The escalation prompt includes that exact
  failure as a worked example. (`ops/text_router.py`, `ops/planner.py`)
- Generalized the classifier's correction-logging table (`label_events`) with a `call_site`
  column so other interpretive calls — starting with agenda extraction — can log their own
  corrections into the same append-only, already-audited table without polluting the
  classifier's own retrain accounting. (`ops/db.py`, `ops/logs.py`)

## 2026-08-23

### Added

- Markdown-to-HTML conversion for Telegram message formatting.

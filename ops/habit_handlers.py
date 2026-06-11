"""Habits — a tracking-domain plugin.

Owns the whole vertical: its own SQLite table (plugins own their schema — the
table is created here, not in core db.py), its data access, the Telegram
checklist + CRUD, and semantic free-text matching.

The `habits` table is the single source of truth — no markdown round-trip. The
planner gets the schedule via `habit_tracker.format_habits_for_prompt(db)`,
generated fresh from the table at prompt time. Notes are added from Telegram
(`/habitnote`) into the `habit_notes` table, not edited in Obsidian files.
"""

import html
import re

import anthropic
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from context import Context
from habit_tracker import (
    compute_streak,
    load_habit_logs,
    missed_last_due_day,
    recent_chain,
)
from logs import Logs
from media import send_sticker
from shabbat import Shabbat
from tg_common import safe_answer


def _match_key(s: str) -> str:
    """Normalise a habit name for forgiving exact-match: lowercase, drop parenthetical
    schedule annotations like "(07:00–08:00)", and strip surrounding punctuation/space.

    Lets `/identity` and `/habitcue` match what a person naturally types ("Shacharit",
    "eat at least 100 grams of protein") against the stored name ("Shacharit (07:00–08:00)",
    "Eat at least 100 grams of protein.") without an LLM. Transliterations the model still
    handles ("Shachris" → "Shacharit") fall through to match_habit.
    """
    s = re.sub(r"\s*\([^)]*\)", "", s.lower())
    return s.strip(" \t.,!?;:—–-")


_HABITS_DDL = """
CREATE TABLE IF NOT EXISTS habits (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    section  TEXT NOT NULL,
    name     TEXT NOT NULL,
    days     TEXT NOT NULL DEFAULT '',   -- CSV of weekday ints (0=Mon..6=Sun); '' = every day
    tracked  INTEGER NOT NULL DEFAULT 1, -- 0 = kept for context only (e.g. "Always off")
    position INTEGER NOT NULL DEFAULT 0,
    cue      TEXT NOT NULL DEFAULT '',   -- implementation intention / habit-stack anchor
    identity TEXT NOT NULL DEFAULT ''    -- the identity this habit casts a vote for
);
"""

# Columns added after the table's original shape; backfilled idempotently on startup.
_ADDED_COLUMNS = {"cue": "''", "identity": "''"}

_HABIT_NOTES_DDL = """
CREATE TABLE IF NOT EXISTS habit_notes (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,
    date  TEXT NOT NULL,
    habit TEXT NOT NULL,   -- canonical habit name the note is about
    note  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habit_notes_habit ON habit_notes(habit);
"""

# Identity is many-to-many: a habit can vote for several identities, and an identity is
# reinforced by several habits. This join table is the source of truth; the dormant
# habits.identity column is kept in sync as a denormalised comma-joined cache so existing
# string readers (struggling_habits, the strategy prompt) keep working untouched.
_HABIT_IDENTITIES_DDL = """
CREATE TABLE IF NOT EXISTS habit_identities (
    habit_id INTEGER NOT NULL,
    identity TEXT NOT NULL,
    PRIMARY KEY (habit_id, identity)
);
CREATE INDEX IF NOT EXISTS idx_habit_identities_identity ON habit_identities(identity);
"""

_NEGATIVE_HABITS_DDL = """
CREATE TABLE IF NOT EXISTS negative_habits (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);
"""

_SLIP_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS slip_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    date    TEXT NOT NULL,
    habit   TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_slip_logs_habit ON slip_logs(habit);
"""

_HABIT_SUGGESTIONS_DDL = """
CREATE TABLE IF NOT EXISTS habit_suggestions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    habit   TEXT NOT NULL,
    display TEXT NOT NULL,
    action  TEXT NOT NULL,
    value   TEXT NOT NULL DEFAULT '{}',
    status  TEXT NOT NULL DEFAULT 'pending'
);
"""

_INT_TO_ABBR = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _days_to_csv(days: list[int] | None) -> str:
    return ",".join(str(d) for d in days) if days else ""


def _csv_to_days(csv: str) -> list[int] | None:
    vals = [int(d) for d in csv.split(",") if d != ""]
    return vals or None


class HabitStore:
    """Habit definitions in SQLite. The habits plugin creates and owns this table."""

    def __init__(self, db, context: Context) -> None:
        self.db = db
        self.context = context
        self.db.ensure_schema(_HABITS_DDL)
        self.db.ensure_schema(_HABIT_NOTES_DDL)
        self.db.ensure_schema(_HABIT_IDENTITIES_DDL)
        self.db.ensure_schema(_NEGATIVE_HABITS_DDL)
        self.db.ensure_schema(_SLIP_LOGS_DDL)
        self.db.ensure_schema(_HABIT_SUGGESTIONS_DDL)
        self._migrate_cue_column()
        self._migrate_identities_to_join()

    def _migrate_cue_column(self) -> None:
        """Backfill columns added after the original habits table shape (idempotent)."""
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(habits)")}
        for col, default in _ADDED_COLUMNS.items():
            if col not in cols:
                self.db.execute(
                    f"ALTER TABLE habits ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
                )

    def _migrate_identities_to_join(self) -> None:
        """One-time backfill of the single-identity column into the M2M join table.

        Idempotent: INSERT OR IGNORE, and any value is split on commas so a previously
        comma-joined cache round-trips cleanly. The column itself is left in place (it
        becomes the denormalised cache `_sync_identity_cache` maintains).
        """
        for r in self.db.query("SELECT id, identity FROM habits WHERE identity != ''"):
            for ident in (p.strip() for p in r["identity"].split(",")):
                if ident:
                    self.db.execute(
                        "INSERT OR IGNORE INTO habit_identities (habit_id, identity) "
                        "VALUES (?, ?)",
                        (r["id"], ident),
                    )

    # --- Reads ---

    def list_habits(self, tracked_only: bool = True) -> list[dict]:
        rows = self.db.query("SELECT * FROM habits ORDER BY position, id")
        out = []
        for r in rows:
            if tracked_only and not r["tracked"]:
                continue
            out.append(
                {
                    "id": r["id"],
                    "section": r["section"],
                    "name": r["name"],
                    "days": _csv_to_days(r["days"]),
                    "tracked": bool(r["tracked"]),
                    "cue": (r["cue"] if "cue" in r.keys() else "") or "",
                    "identity": (r["identity"] if "identity" in r.keys() else "") or "",
                    "identities": self._identities(r["id"]),
                }
            )
        return out

    def sections(self) -> dict[str, list[dict]]:
        """{section: [habit dicts]} for tracked habits — checklist shape, order preserved."""
        secs: dict[str, list[dict]] = {}
        for h in self.list_habits(tracked_only=True):
            secs.setdefault(h["section"], []).append(h)
        return secs

    # --- Writes (CRUD) ---

    def add(
        self, name: str, section: str = "Habits", days: list[int] | None = None
    ) -> None:
        nxt = self.db.query("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM habits")[
            0
        ]["p"]
        self.db.execute(
            "INSERT INTO habits (section, name, days, tracked, position) VALUES (?, ?, ?, 1, ?)",
            (section, name.strip(), _days_to_csv(days), nxt),
        )

    def remove(self, habit_id: int) -> None:
        self.db.execute("DELETE FROM habit_identities WHERE habit_id = ?", (habit_id,))
        self.db.execute("DELETE FROM habits WHERE id = ?", (habit_id,))

    def set_tracked(self, habit_id: int, tracked: bool) -> None:
        self.db.execute(
            "UPDATE habits SET tracked = ? WHERE id = ?",
            (1 if tracked else 0, habit_id),
        )

    def _set_field_by_name(self, field: str, name: str, value: str) -> str | None:
        """Set `field` on the habit whose display/raw name matches (case-insensitive)."""
        target = _match_key(name)
        for h in self.list_habits(tracked_only=False):
            display = self.context.habit_display_name(h["name"])
            if _match_key(display) == target or _match_key(h["name"]) == target:
                self.db.execute(
                    f"UPDATE habits SET {field} = ? WHERE id = ?",
                    (value.strip(), h["id"]),
                )
                return display
        return None

    def set_cue_by_name(self, name: str, cue: str) -> str | None:
        """Set the cue (implementation intention / stack anchor). Returns matched name or None."""
        return self._set_field_by_name("cue", name, cue)

    # --- Identity (many-to-many: a habit votes for several identities) ---

    def _habit_by_name(self, name: str) -> dict | None:
        """The habit whose display/raw name matches `name` (forgiving), or None."""
        target = _match_key(name)
        for h in self.list_habits(tracked_only=False):
            display = self.context.habit_display_name(h["name"])
            if _match_key(display) == target or _match_key(h["name"]) == target:
                return h
        return None

    def _identities(self, habit_id: int) -> list[str]:
        rows = self.db.query(
            "SELECT identity FROM habit_identities WHERE habit_id = ? ORDER BY identity",
            (habit_id,),
        )
        return [r["identity"] for r in rows]

    def _sync_identity_cache(self, habit_id: int) -> None:
        """Rewrite the denormalised habits.identity cache from the join table."""
        joined = ", ".join(self._identities(habit_id))
        self.db.execute(
            "UPDATE habits SET identity = ? WHERE id = ?", (joined, habit_id)
        )

    def add_identities(self, name: str, identities: list[str]) -> str | None:
        """Add (accumulate) identities a habit votes for. Returns matched name or None."""
        h = self._habit_by_name(name)
        if not h:
            return None
        for ident in identities:
            ident = ident.strip()
            if ident:
                self.db.execute(
                    "INSERT OR IGNORE INTO habit_identities (habit_id, identity) "
                    "VALUES (?, ?)",
                    (h["id"], ident),
                )
        self._sync_identity_cache(h["id"])
        return self.context.habit_display_name(h["name"])

    def remove_identity(self, name: str, identity: str) -> str | None:
        """Remove one identity from a habit. Returns matched name or None (no-op if absent)."""
        h = self._habit_by_name(name)
        if not h:
            return None
        self.db.execute(
            "DELETE FROM habit_identities WHERE habit_id = ? AND LOWER(identity) = LOWER(?)",
            (h["id"], identity.strip()),
        )
        self._sync_identity_cache(h["id"])
        return self.context.habit_display_name(h["name"])

    def identities_of(self, name: str) -> list[str] | None:
        """A habit's current identities, or None if the name doesn't match a habit."""
        h = self._habit_by_name(name)
        return self._identities(h["id"]) if h else None

    # --- Notes (added via the bot, stored in the DB) ---

    def add_note(self, habit_name: str, note: str) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Jerusalem"))
        self.db.execute(
            "INSERT INTO habit_notes (ts, date, habit, note) VALUES (?, ?, ?, ?)",
            (
                now.isoformat(timespec="seconds"),
                now.date().isoformat(),
                habit_name,
                note.strip(),
            ),
        )

    def notes_for(self, habit_name: str, limit: int = 10) -> list[dict]:
        rows = self.db.query(
            "SELECT date, note FROM habit_notes WHERE LOWER(habit) = LOWER(?) "
            "ORDER BY id DESC LIMIT ?",
            (habit_name.strip(), limit),
        )
        return [{"date": r["date"], "note": r["note"]} for r in rows]

    def recent_notes(self, days: int = 7) -> list[dict]:
        from datetime import date as _date, timedelta as _td

        start = (_date.today() - _td(days=days)).isoformat()
        rows = self.db.query(
            "SELECT date, habit, note FROM habit_notes WHERE date >= ? ORDER BY id",
            (start,),
        )
        return [
            {"date": r["date"], "habit": r["habit"], "note": r["note"]} for r in rows
        ]

    # --- CRUD: edit name / schedule / section ---

    def rename(self, name: str, new_name: str) -> str | None:
        """Rename a habit. Returns old display name or None if not found."""
        return self._set_field_by_name("name", name, new_name.strip())

    def set_days_by_name(self, name: str, days: list[int] | None) -> str | None:
        """Update a habit's schedule. Returns display name or None."""
        return self._set_field_by_name("days", name, _days_to_csv(days))

    def set_section_by_name(self, name: str, section: str) -> str | None:
        """Move a habit to a different section. Returns display name or None."""
        return self._set_field_by_name("section", name, section.strip())

    # --- Negative habits (things to track slipping on) ---

    def add_negative_habit(self, name: str) -> None:
        nxt = self.db.query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM negative_habits"
        )[0]["p"]
        self.db.execute(
            "INSERT OR IGNORE INTO negative_habits (name, position) VALUES (?, ?)",
            (name.strip(), nxt),
        )

    def list_negative_habits(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, name FROM negative_habits ORDER BY position, id"
        )
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def remove_negative_habit(self, habit_id: int) -> None:
        self.db.execute("DELETE FROM negative_habits WHERE id = ?", (habit_id,))

    # --- Slip logs (negative habits — no judgement) ---

    def log_slip(self, habit: str, note: str = "") -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Jerusalem"))
        self.db.execute(
            "INSERT INTO slip_logs (ts, date, habit, note) VALUES (?, ?, ?, ?)",
            (
                now.isoformat(timespec="seconds"),
                now.date().isoformat(),
                habit.strip(),
                note.strip(),
            ),
        )

    def recent_slips(self, habit: str | None = None, days: int = 30) -> list[dict]:
        from datetime import date as _date, timedelta as _td

        start = (_date.today() - _td(days=days)).isoformat()
        if habit:
            rows = self.db.query(
                "SELECT date, habit, note FROM slip_logs "
                "WHERE date >= ? AND LOWER(habit) = LOWER(?) ORDER BY id",
                (start, habit.strip()),
            )
        else:
            rows = self.db.query(
                "SELECT date, habit, note FROM slip_logs WHERE date >= ? ORDER BY id",
                (start,),
            )
        return [
            {"date": r["date"], "habit": r["habit"], "note": r["note"]} for r in rows
        ]

    # --- Weekly suggestions ---

    def save_suggestion(
        self, habit: str, display: str, action: str, value: dict
    ) -> int:
        import json
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Jerusalem"))
        self.db.execute(
            "INSERT INTO habit_suggestions (ts, habit, display, action, value) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                now.isoformat(timespec="seconds"),
                habit,
                display,
                action,
                json.dumps(value),
            ),
        )
        return self.db.query("SELECT last_insert_rowid() AS id")[0]["id"]

    def get_suggestion(self, suggestion_id: int) -> dict | None:
        import json

        rows = self.db.query(
            "SELECT * FROM habit_suggestions WHERE id = ?", (suggestion_id,)
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "habit": r["habit"],
            "display": r["display"],
            "action": r["action"],
            "value": json.loads(r["value"]),
            "status": r["status"],
        }

    def update_suggestion_status(self, suggestion_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE habit_suggestions SET status = ? WHERE id = ?",
            (status, suggestion_id),
        )


async def match_habit(content: str, db) -> str | None:
    """Resolve a free-text habit log to the canonical habit name it satisfies, or None.

    Reads the habit names from the table, short-circuits when the text already is a
    habit name (no model call), else asks the cheapest model to pick semantically
    (e.g. "took a stroll" -> "Daily walk"), constrained to the actual habit names.
    """
    rows = db.query("SELECT name FROM habits WHERE tracked = 1")
    names = [Context.habit_display_name(r["name"]) for r in rows]
    if not names:
        return None
    by_lower = {n.strip().lower(): n for n in names}
    if content.strip().lower() in by_lower:
        return by_lower[content.strip().lower()]  # already a habit name — no model call

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        tools=[
            {
                "name": "match_habit",
                "description": "Pick which habit a free-text log entry satisfies, or 'none'.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "habit": {
                            "type": "string",
                            "enum": [*names, "none"],
                            "description": "The habit this entry satisfies, or 'none' if it matches no habit.",
                        },
                    },
                    "required": ["habit"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "match_habit"},
        messages=[
            {
                "role": "user",
                "content": f"Habits: {names}\nLog entry: {content!r}\nWhich habit does this satisfy?",
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            choice = block.input.get("habit")
            return None if choice in (None, "none") else choice
    return None


async def match_slip(content: str, db) -> str | None:
    """Resolve free-text slip to a canonical negative habit name, or None.

    Exact match first (no model call), then LLM semantic match so
    'slept in', 'woke up late', 'got up at 10' all resolve to 'Late wake'.
    Returns None when there are no negative habits defined or nothing matches.
    """
    rows = db.query("SELECT name FROM negative_habits ORDER BY position, id")
    names = [r["name"] for r in rows]
    if not names:
        return None
    by_lower = {n.strip().lower(): n for n in names}
    if content.strip().lower() in by_lower:
        return by_lower[content.strip().lower()]

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        tools=[
            {
                "name": "match_slip",
                "description": "Pick which negative habit a free-text slip describes, or 'none'.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "habit": {
                            "type": "string",
                            "enum": [*names, "none"],
                            "description": "The negative habit this slip describes, or 'none'.",
                        },
                    },
                    "required": ["habit"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "match_slip"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Negative habits: {names}\n"
                    f"Slip description: {content!r}\n"
                    "Which negative habit does this describe?"
                ),
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            choice = block.input.get("habit")
            return None if choice in (None, "none") else choice
    return None


class HabitHandlers:
    def __init__(
        self, bot: Bot, logs: Logs, context: Context, allowed_user: int, planner=None
    ) -> None:
        self.bot = bot
        self.logs = logs
        self.context = context
        self.allowed_user = allowed_user
        self.planner = planner  # for the failing-habit strategy (4-Laws) call
        self.shabbat = Shabbat(
            logs.log_dir
        )  # to know when it's actually Shabbat vs motzei
        self.store = HabitStore(logs.db, context)  # plugin creates/owns its table here
        # Scheduled jobs this plugin contributes (the registry collects these).
        self.jobs = [
            {
                "id": "habit_eod_check",
                "func": self.daily_habit_check,
                "trigger": "cron",
                "kwargs": {"hour": 23, "minute": 30},
            },
            {
                "id": "habit_weekly_suggestions",
                "func": self.weekly_habit_suggestions,
                "trigger": "cron",
                "kwargs": {"day_of_week": "sun", "hour": 9, "minute": 0},
            },
        ]

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("habits", self.cmd_habits))
        app.add_handler(CommandHandler("h", self.cmd_habits))
        app.add_handler(CommandHandler("addhabit", self.cmd_add_habit))
        app.add_handler(CommandHandler("edithabit", self.cmd_edit_habit))
        app.add_handler(CommandHandler("habitcue", self.cmd_habit_cue))
        app.add_handler(CommandHandler("habitnote", self.cmd_habit_note))
        app.add_handler(CommandHandler("identity", self.cmd_identity))
        app.add_handler(CommandHandler("habitstrategy", self.cmd_habit_strategy))
        app.add_handler(
            CommandHandler("weeklyhabits", self.cmd_weekly_habit_suggestions)
        )
        app.add_handler(CommandHandler("managehabits", self.cmd_manage))
        app.add_handler(CommandHandler("habitcheck", self.cmd_habit_check))
        app.add_handler(CommandHandler("addslip", self.cmd_add_slip))
        app.add_handler(CommandHandler("manageslips", self.cmd_manage_slips))
        app.add_handler(CommandHandler("slip", self.cmd_slip))
        app.add_handler(CommandHandler("slips", self.cmd_slips))
        app.add_handler(
            CallbackQueryHandler(self.handle_manage_slips, pattern="^hbsl_del:")
        )
        app.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^hb_done:"))
        app.add_handler(
            CallbackQueryHandler(self.handle_manage, pattern="^hb_(del|on|off):")
        )
        app.add_handler(
            CallbackQueryHandler(self.handle_eod, pattern="^hbq_(done|miss):")
        )
        app.add_handler(
            CallbackQueryHandler(
                self.handle_suggestion, pattern="^hbs_(accept|reject):"
            )
        )

    # --- Trackable capability ---

    def summary(self, days: int) -> str:
        """Current habit streaks within the window — for the digest / eval harness."""
        habits = self.store.list_habits(tracked_only=True)
        if not habits:
            return ""
        parts = []
        for h in habits:
            current, _ = compute_streak(self.logs, h["name"], lookback=max(days, 1))
            disp = self.context.habit_display_name(h["name"])
            parts.append(f"{disp} 🔥{current}" if current else f"{disp} —")
        return f"Habit streaks (last {days}d): " + ", ".join(parts)

    # --- Checklist rendering ---

    def _resolve_logged_to_habit(self, logged: str, all_habits: list) -> dict | None:
        """Map a logged entry to its habit by exact display-name match (both write
        paths store the canonical name, so exact match is all that's needed)."""
        logged_l = logged.strip().lower()
        for h in all_habits:
            if logged_l == self.context.habit_display_name(h["name"]).strip().lower():
                return h
        return None

    def _message(self) -> tuple[str, InlineKeyboardMarkup]:
        from datetime import date as _date

        # Only suppress the list while it's actually Shabbat — once it's out (motzei
        # shabbat, Saturday night), the list comes back so the week can start.
        if self.shabbat.quiet_now():
            return (
                "🕯 <b>Shabbat</b> — habits aren't tracked now, and Shabbat never counts "
                "against a streak. Rest.",
                InlineKeyboardMarkup([]),
            )
        today_weekday = _date.today().weekday()
        sections = self.store.sections()
        logged_today = [
            e["content"].strip()
            for e in self.logs.read_today()
            if e.get("tag") == "habit"
        ]

        all_visible = []
        for habits in sections.values():
            all_visible.extend(
                h for h in habits if h["days"] is None or today_weekday in h["days"]
            )
        done_ids = set()
        for logged in logged_today:
            h = self._resolve_logged_to_habit(logged, all_visible)
            if h:
                done_ids.add(h["id"])

        logged_by_day = load_habit_logs(self.logs)  # one DB read, shared by all habits

        lines = ["📋 <b>Habits</b>\n"]
        rows = []
        at_risk_any = False
        for section, habits in sections.items():
            visible = [
                h for h in habits if h["days"] is None or today_weekday in h["days"]
            ]
            if not visible:
                continue
            lines.append(f"<b>{html.escape(section)}</b>")
            for h in visible:
                name = self.context.habit_display_name(h["name"])
                done = h["id"] in done_ids
                cur, _ = compute_streak(
                    self.logs,
                    h["name"],
                    due_weekdays=h["days"],
                    logged_by_day=logged_by_day,
                )
                chain = "".join(
                    "🟩" if x else "⬜"
                    for x in recent_chain(
                        self.logs,
                        h["name"],
                        h["days"],
                        n=10,
                        logged_by_day=logged_by_day,
                    )
                )
                at_risk = (not done) and missed_last_due_day(
                    self.logs, h["name"], h["days"], logged_by_day=logged_by_day
                )
                at_risk_any = at_risk_any or at_risk
                flame = f"  🔥{cur}" if cur else ""
                warn = "  ⚠️ don't miss twice" if at_risk else ""
                lines.append(
                    f"{'✅' if done else '⬜'} {html.escape(name)}{flame}{warn}"
                )
                sub = chain
                if h["cue"]:
                    sub += f"  ↳ {html.escape(h['cue'])}"
                if sub:
                    lines.append(sub)
                if not done:
                    key = name[:52]  # callback_data max 64 bytes; "hb_done:" = 8
                    label = ("⚠️ " if at_risk else "✅ ") + name
                    rows.append(
                        [InlineKeyboardButton(label, callback_data=f"hb_done:{key}")]
                    )
            lines.append("")

        if at_risk_any:
            lines.append(
                "⚠️ = missed last time. Don't miss twice — that's how chains die."
            )

        return "\n".join(lines).strip(), InlineKeyboardMarkup(rows)

    # --- End-of-day check-in ---

    def _pending_today_habits(self) -> list[dict]:
        """Habit rows due today that have neither a done nor a missed log yet.
        The shared core of the end-of-day check and the /status snapshot."""
        from datetime import date as _date

        today_weekday = _date.today().weekday()
        sections = self.store.sections()
        resolved = [
            e["content"].strip()
            for e in self.logs.read_today()
            if e.get("tag") in ("habit", "habit_missed")
        ]

        all_visible = []
        for habits in sections.values():
            all_visible.extend(
                h for h in habits if h["days"] is None or today_weekday in h["days"]
            )
        resolved_ids = set()
        for logged in resolved:
            h = self._resolve_logged_to_habit(logged, all_visible)
            if h:
                resolved_ids.add(h["id"])
        return [h for h in all_visible if h["id"] not in resolved_ids]

    def pending_today(self) -> list[str]:
        """Display names of today's habits not yet logged done or missed — the
        'open habits' the /status snapshot lists. Empty list means all accounted
        for. Does not consider Shabbat; the caller suppresses it then."""
        return [
            self.context.habit_display_name(h["name"])
            for h in self._pending_today_habits()
        ]

    def _eod_message(self) -> tuple[str | None, InlineKeyboardMarkup | None]:
        """Prompt for habits due today that have neither a done nor a missed log yet.
        Returns (None, None) when nothing is pending."""
        pending = self._pending_today_habits()
        if not pending:
            return None, None

        lines = ["🌙 <b>End-of-day habit check</b>", "Did you do these today?", ""]
        rows = []
        for h in pending:
            name = self.context.habit_display_name(h["name"])
            lines.append(f"⬜ {html.escape(name)}")
            key = name[:48]  # callback_data budget: "hbq_done:" is 9 bytes
            rows.append(
                [
                    InlineKeyboardButton(
                        f"✅ {name[:22]}", callback_data=f"hbq_done:{key}"
                    ),
                    InlineKeyboardButton("❌ Didn't", callback_data=f"hbq_miss:{key}"),
                ]
            )
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def daily_habit_check(self, force: bool = False) -> None:
        """Scheduled 23:30 prompt asking whether today's still-unlogged habits got done.
        Skips Fri/Sat (Shabbat — habits aren't tracked) and stays silent if nothing's
        pending. `force` bypasses the Shabbat skip (used by the preview fire)."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if not force and datetime.now(ZoneInfo("Asia/Jerusalem")).weekday() in (4, 5):
            return
        text, keyboard = self._eod_message()
        if text is None:
            return
        await send_sticker(self.bot, self.allowed_user, "winddown")
        await self.bot.send_message(
            chat_id=self.allowed_user,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def cmd_habit_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Trigger the end-of-day habit check on demand."""
        if update.effective_user.id != self.allowed_user:
            return
        text, keyboard = self._eod_message()
        if text is None:
            await update.message.reply_text(
                "✅ Every habit due today is already accounted for."
            )
        else:
            await update.message.reply_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )

    async def handle_eod(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await safe_answer(query)
        action, name = query.data.split(":", 1)
        self.logs.write("habit" if action == "hbq_done" else "habit_missed", name)
        await send_sticker(
            self.bot,
            update.effective_chat.id,
            "done" if action == "hbq_done" else "missed",
        )
        text, keyboard = self._eod_message()
        if text is None:
            await query.edit_message_text(
                "🌙 End-of-day check done — every habit accounted for. Good night."
            )
        else:
            await query.edit_message_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )

    # --- Handlers: checklist + logging ---

    async def cmd_habits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        if not self.store.list_habits():
            await update.message.reply_text(
                "No habits yet. Add one with <code>/addhabit Drink water</code>.",
                parse_mode="HTML",
            )
            return
        text, keyboard = self._message()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await safe_answer(query)
        habit_name = query.data.split(":", 1)[1]
        self.logs.write("habit", habit_name)
        # Celebrate milestones on the checklist (not every tap — that'd be spam). 3 is the
        # early "don't break the chain" win; then the usual 1/4/15-week-ish marks.
        cur, _ = compute_streak(self.logs, habit_name)
        if cur in (3, 7, 30, 100, 365):
            await send_sticker(self.bot, update.effective_chat.id, "streak")
        text, keyboard = self._message()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def cmd_habit_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/habitnote <habit>: <note> — attach a dated note to a habit.
        /habitnote <habit> — show that habit's recent notes. /habitnote — all recent."""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""

        if ":" in raw:  # add a note
            name, note = raw.split(":", 1)
            display = self._resolve_habit_name(name.strip())
            if not display:
                await update.message.reply_text(
                    f"No habit matching “{html.escape(name.strip())}”. /habits to see names."
                )
                return
            self.store.add_note(display, note.strip())
            await update.message.reply_text(
                f"📝 Noted on <b>{html.escape(display)}</b>: {html.escape(note.strip())}",
                parse_mode="HTML",
            )
            return

        if raw:  # show one habit's notes
            display = self._resolve_habit_name(raw)
            if not display:
                await update.message.reply_text(
                    f"No habit matching “{html.escape(raw)}”. /habits to see names."
                )
                return
            notes = self.store.notes_for(display)
            if not notes:
                await update.message.reply_text(
                    f"No notes yet for {html.escape(display)}."
                )
                return
            lines = [f"📝 <b>{html.escape(display)}</b> — recent notes\n"]
            lines += [
                f"<code>{n['date']}</code> {html.escape(n['note'])}" for n in notes
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        # no args — recent notes across all habits
        notes = self.store.recent_notes(days=14)
        if not notes:
            await update.message.reply_text(
                "No habit notes yet. Add one: <code>/habitnote Strength: shoulder felt off</code>",
                parse_mode="HTML",
            )
            return
        lines = ["📝 <b>Recent habit notes</b>\n"]
        lines += [
            f"<code>{n['date']}</code> {html.escape(n['habit'])}: {html.escape(n['note'])}"
            for n in notes[-15:]
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    def _resolve_habit_name(self, name: str) -> str | None:
        """Display name of the tracked/untracked habit matching `name`, or None."""
        target = _match_key(name)
        for h in self.store.list_habits(tracked_only=False):
            display = self.context.habit_display_name(h["name"])
            if _match_key(display) == target or _match_key(h["name"]) == target:
                return display
        return None

    async def _resolve_or_match(self, name: str) -> str | None:
        """Typed habit name → canonical display name. Forgiving exact match first (no
        LLM), then the semantic match_habit resolver for transliterations/paraphrases
        ("Shachris" → "Shacharit (07:00–08:00)") — the same resolver habit logging uses."""
        direct = self._resolve_habit_name(name)
        if direct:
            return direct
        try:
            return await match_habit(name, self.logs.db)
        except Exception:
            return None

    # --- Handlers: CRUD ---

    async def cmd_add_habit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/addhabit <name> [days]  — e.g. /addhabit Stretch [mon,wed,fri]"""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip()
        if not raw:
            await update.message.reply_text(
                "Usage: <code>/addhabit Drink water [mon,wed,fri]</code>",
                parse_mode="HTML",
            )
            return
        days = None
        tag_m = re.search(r"\[([^\]]+)\]$", raw)
        if tag_m:
            days = [
                Context._DAY_NAMES[d.strip()]
                for d in tag_m.group(1).split(",")
                if d.strip() in Context._DAY_NAMES
            ] or None
            raw = raw[: tag_m.start()].strip()
        self.store.add(raw, days=days)
        await update.message.reply_text(
            f"➕ Added habit: <b>{html.escape(raw)}</b>", parse_mode="HTML"
        )

    async def cmd_edit_habit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/edithabit <name>: field=value — edit name, days, or section.

        /edithabit Stretch: name=Quick stretch (2 min)
        /edithabit Stretch: days=mon,wed,fri
        /edithabit Stretch: days=daily
        /edithabit Stretch: section=Morning
        /edithabit Stretch   (no colon — shows current state)
        """
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        if not raw:
            await update.message.reply_text(
                "Usage: <code>/edithabit &lt;name&gt;: field=value</code>\n"
                "Fields: <code>name</code>, <code>days</code> (e.g. mon,wed,fri or daily), "
                "<code>section</code>",
                parse_mode="HTML",
            )
            return

        if ":" not in raw:
            target = _match_key(raw)
            habit = None
            for h in self.store.list_habits(tracked_only=False):
                disp = self.context.habit_display_name(h["name"])
                if _match_key(disp) == target or _match_key(h["name"]) == target:
                    habit = h
                    break
            if not habit:
                await update.message.reply_text(
                    f"No habit matching “{html.escape(raw)}”."
                )
                return
            disp = self.context.habit_display_name(habit["name"])
            days_str = (
                ", ".join(_INT_TO_ABBR[d] for d in sorted(habit["days"]))
                if habit["days"]
                else "daily"
            )
            await update.message.reply_text(
                f"<b>{html.escape(disp)}</b>\n"
                f"Section: {html.escape(habit['section'])}\n"
                f"Days: {days_str}\n"
                f"Tracked: {'yes' if habit['tracked'] else 'no'}\n"
                f"Cue: {html.escape(habit['cue'] or '—')}\n\n"
                f"To edit: <code>/edithabit {html.escape(raw)}: name=New name</code>",
                parse_mode="HTML",
            )
            return

        name, rest = raw.split(":", 1)
        name = name.strip()
        rest = rest.strip()
        if "=" not in rest:
            await update.message.reply_text(
                "Use <code>field=value</code> — e.g. <code>name=New name</code>, "
                "<code>days=mon,wed,fri</code>, <code>section=Morning</code>",
                parse_mode="HTML",
            )
            return

        field, value = rest.split("=", 1)
        field = field.strip().lower()
        value = value.strip()

        if field == "name":
            matched = self.store.rename(name, value)
            if matched:
                await update.message.reply_text(
                    f"✏️ <b>{html.escape(matched)}</b> → <b>{html.escape(value)}</b>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"No habit matching “{html.escape(name)}”."
                )

        elif field == "days":
            if value.lower() == "daily":
                days = None
            else:
                days = [
                    Context._DAY_NAMES[d.strip()]
                    for d in value.split(",")
                    if d.strip() in Context._DAY_NAMES
                ] or None
                if days is None:
                    await update.message.reply_text(
                        "Unrecognised days. Use: mon, tue, wed, thu, fri, sat, sun or 'daily'."
                    )
                    return
            matched = self.store.set_days_by_name(name, days)
            if matched:
                days_str = (
                    ", ".join(_INT_TO_ABBR[d] for d in sorted(days))
                    if days
                    else "daily"
                )
                await update.message.reply_text(
                    f"📅 <b>{html.escape(matched)}</b> → {days_str}",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"No habit matching “{html.escape(name)}”."
                )

        elif field == "section":
            matched = self.store.set_section_by_name(name, value)
            if matched:
                await update.message.reply_text(
                    f"📂 <b>{html.escape(matched)}</b> → section: {html.escape(value)}",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"No habit matching “{html.escape(name)}”."
                )

        else:
            await update.message.reply_text(
                f"Unknown field <code>{html.escape(field)}</code>. Use: name, days, or section.",
                parse_mode="HTML",
            )

    async def cmd_habit_cue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/habitcue <habit>: <cue> — set an implementation intention / stack anchor.

        e.g. /habitcue Daf Yomi: after Maariv, 21:00 at the beis
        With no args (or no colon) it lists each habit's current cue.
        """
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        if ":" not in raw:
            lines = ["🔗 <b>Habit cues</b> (when/where/after)\n"]
            for h in self.store.list_habits(tracked_only=False):
                disp = self.context.habit_display_name(h["name"])
                cue = h["cue"] or "—"
                lines.append(f"• {html.escape(disp)}: {html.escape(cue)}")
            lines.append(
                "\nSet one: <code>/habitcue Daf Yomi: after Maariv, 21:00</code>"
            )
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return
        name, cue = raw.split(":", 1)
        matched = await self._resolve_or_match(name.strip())
        if matched:
            self.store.set_cue_by_name(matched, cue.strip())
            await update.message.reply_text(
                f"🔗 <b>{html.escape(matched)}</b> → <i>{html.escape(cue.strip())}</i>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"No habit matching “{html.escape(name.strip())}”. Try /habits to see names."
            )

    async def cmd_identity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/identity — show habits grouped by the identities they vote for.
        /identity <habit>: <id1>, <id2> — add identities (a habit can vote for several);
        prefix one with '-' to remove it (e.g. 'Strength: -disciplined')."""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        if ":" in raw:
            name, rest = raw.split(":", 1)
            matched = await self._resolve_or_match(name.strip())
            if not matched:
                await update.message.reply_text(
                    f"No habit matching “{html.escape(name.strip())}”. Try /habits."
                )
                return
            # Comma-separated identities; a leading '-' on a token removes it.
            adds = [
                t.strip()
                for t in rest.split(",")
                if t.strip() and not t.strip().startswith("-")
            ]
            removes = [
                t.strip()[1:].strip()
                for t in rest.split(",")
                if t.strip().startswith("-")
            ]
            if adds:
                self.store.add_identities(matched, adds)
            for ident in removes:
                self.store.remove_identity(matched, ident)
            current = self.store.identities_of(matched) or []
            if current:
                votes = ", ".join(html.escape(i) for i in current)
                await update.message.reply_text(
                    f"🪪 <b>{html.escape(matched)}</b> votes for <i>{votes}</i>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"🪪 <b>{html.escape(matched)}</b> votes for no identity yet.",
                    parse_mode="HTML",
                )
            return

        # Grouped "votes" view — a habit appears under each identity it votes for.
        logged_by_day = load_habit_logs(self.logs)
        by_identity: dict[str, list[str]] = {}
        untagged: list[str] = []
        for h in self.store.list_habits(tracked_only=True):
            disp = self.context.habit_display_name(h["name"])
            cur, _ = compute_streak(
                self.logs,
                h["name"],
                due_weekdays=h["days"],
                logged_by_day=logged_by_day,
            )
            label = f"{disp} 🔥{cur}" if cur else disp
            if h["identities"]:
                for ident in h["identities"]:
                    by_identity.setdefault(ident, []).append(label)
            else:
                untagged.append(disp)

        if not by_identity:
            await update.message.reply_text(
                "No identities tagged yet. Every habit is a vote for who you're becoming —\n"
                "tag one: <code>/identity Daf Yomi: Ben Torah</code>",
                parse_mode="HTML",
            )
            return

        lines = ["🪪 <b>Identities</b> — every rep is a vote\n"]
        for ident, habits in by_identity.items():
            votes = sum(int(h.split("🔥")[-1]) for h in habits if "🔥" in h)
            lines.append(f"<b>{html.escape(ident)}</b>  ({votes} active)")
            for h in habits:
                lines.append(f"  • {html.escape(h)}")
            lines.append("")
        if untagged:
            lines.append(f"<i>Untagged: {html.escape(', '.join(untagged))}</i>")
        await update.message.reply_text("\n".join(lines).strip(), parse_mode="HTML")

    async def cmd_habit_strategy(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """/habitstrategy — run a 4-Laws strategy session on chronically-missed habits."""
        if update.effective_user.id != self.allowed_user:
            return
        from habit_tracker import struggling_habits

        strugglers = struggling_habits(self.logs)
        if not strugglers:
            await update.message.reply_text(
                "Nothing chronically slipping — your chains look healthy. 🟩"
            )
            return
        if not self.planner:
            names = ", ".join(s["name"] for s in strugglers)
            await update.message.reply_text(f"Struggling: {html.escape(names)}")
            return
        await update.message.reply_text("🧭 Strategizing…")
        try:
            text = await self.planner.habit_strategy(strugglers)
            await update.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Strategy failed: {e}")

    async def cmd_add_slip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/addslip <name> — define a negative habit to track (e.g. /addslip Late wake)."""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        if not raw:
            await update.message.reply_text(
                "Usage: <code>/addslip Late wake</code>", parse_mode="HTML"
            )
            return
        self.store.add_negative_habit(raw)
        await update.message.reply_text(
            f"➕ Tracking: <b>{html.escape(raw)}</b>\n"
            f"Log with <code>/slip {html.escape(raw)}</code> or any close paraphrase.",
            parse_mode="HTML",
        )

    def _manage_slips_message(self) -> tuple[str, InlineKeyboardMarkup]:
        habits = self.store.list_negative_habits()
        if not habits:
            return (
                "No negative habits defined yet. Add one with /addslip.",
                InlineKeyboardMarkup([]),
            )
        rows = []
        for h in habits:
            rows.append(
                [
                    InlineKeyboardButton(h["name"], callback_data="noop"),
                    InlineKeyboardButton("🗑", callback_data=f"hbsl_del:{h['id']}"),
                ]
            )
        return "⚙️ <b>Negative habits</b> — delete:", InlineKeyboardMarkup(rows)

    async def cmd_manage_slips(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """/manageslips — delete negative habits from the tracking list."""
        if update.effective_user.id != self.allowed_user:
            return
        text, keyboard = self._manage_slips_message()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def handle_manage_slips(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await safe_answer(query)
        _, hid = query.data.split(":", 1)
        self.store.remove_negative_habit(int(hid))
        text, keyboard = self._manage_slips_message()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def cmd_slip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/slip <behavior> [: note] — log a slip; resolves to a tracked negative habit if defined."""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        if not raw:
            defined = self.store.list_negative_habits()
            if defined:
                names = ", ".join(h["name"] for h in defined)
                await update.message.reply_text(
                    f"Usage: <code>/slip Late wake</code> or "
                    f"<code>/slip junk food: stress</code>\n"
                    f"Tracking: {html.escape(names)}",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    "Usage: <code>/slip Late wake</code>\n"
                    "Define what to track first with <code>/addslip Late wake</code>.",
                    parse_mode="HTML",
                )
            return
        habit_raw, note = (raw.split(":", 1) + [""])[:2]
        habit_raw = habit_raw.strip()
        note = note.strip()
        canonical = await match_slip(habit_raw, self.logs.db)
        stored_name = canonical if canonical else habit_raw
        self.store.log_slip(stored_name, note)
        if canonical and canonical.lower() != habit_raw.lower():
            await update.message.reply_text(
                f"Noted as <b>{html.escape(canonical)}</b>. "
                "No judgement — awareness is the first step.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "Noted. No judgement — awareness is the first step."
            )

    async def cmd_slips(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/slips — summary counts by behavior. /slips <name> — detail for one."""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""

        if raw:
            slips = self.store.recent_slips(habit=raw, days=30)
            if not slips:
                canonical = await match_slip(raw, self.logs.db)
                if canonical:
                    slips = self.store.recent_slips(habit=canonical, days=30)
                    raw = canonical
            if not slips:
                await update.message.reply_text(
                    f'No slips for "{html.escape(raw)}" in the last 30 days.'
                )
                return
            lines = [f"📋 <b>{html.escape(raw)}</b> — last 30 days\n"]
            for s in slips[-20:]:
                note_part = f" — {html.escape(s['note'])}" if s["note"] else ""
                lines.append(f"<code>{s['date']}</code>{note_part}")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        all_slips = self.store.recent_slips(days=30)
        if not all_slips:
            await update.message.reply_text("No slips logged in the last 30 days.")
            return
        counts: dict[str, dict] = {}
        for s in all_slips:
            name = s["habit"]
            if name not in counts:
                counts[name] = {"count": 0, "last": s["date"]}
            counts[name]["count"] += 1
            counts[name]["last"] = max(counts[name]["last"], s["date"])
        lines = ["📋 <b>Slips — last 30 days</b>\n"]
        for name, stat in sorted(counts.items(), key=lambda x: -x[1]["count"]):
            lines.append(
                f"{html.escape(name)} — {stat['count']}× (last: {stat['last']})"
            )
        lines.append("\n<i>/slips &lt;name&gt; for detail</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def weekly_habit_suggestions(self) -> None:
        """Sunday 09:00 — send Atomic Habits suggestions for struggling habits."""
        from habit_tracker import struggling_habits

        strugglers = struggling_habits(self.logs)
        if not strugglers or not self.planner:
            return
        try:
            suggestions = await self.planner.habit_weekly_suggestions(strugglers)
        except Exception:
            return
        _ACTION_LABEL = {
            "set_cue": "✅ Add cue",
            "set_days": "✅ Adjust schedule",
            "rename": "✅ Rename habit",
            "archive": "✅ Archive for now",
        }
        for s in suggestions:
            habit = s.get("habit", "")
            display = s.get("display", "")
            action = s.get("action", "")
            value = s.get("value", {})
            if not habit or not display or not action:
                continue
            sid = self.store.save_suggestion(habit, display, action, value)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            _ACTION_LABEL.get(action, "✅ Apply"),
                            callback_data=f"hbs_accept:{sid}",
                        ),
                        InlineKeyboardButton("Skip", callback_data=f"hbs_reject:{sid}"),
                    ]
                ]
            )
            await self.bot.send_message(
                chat_id=self.allowed_user,
                text=f"💡 <b>Weekly habit tip</b>\n\n{html.escape(display)}",
                parse_mode="HTML",
                reply_markup=keyboard,
            )

    async def cmd_weekly_habit_suggestions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Manual trigger for the weekly habit suggestions (for testing / on-demand)."""
        if update.effective_user.id != self.allowed_user:
            return
        await update.message.reply_text("🔍 Checking for struggling habits…")
        await self.weekly_habit_suggestions()

    async def handle_suggestion(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await safe_answer(query)
        action_str, sid_str = query.data.split(":", 1)
        suggestion_id = int(sid_str)
        sugg = self.store.get_suggestion(suggestion_id)
        if not sugg:
            await query.edit_message_text("Suggestion not found.")
            return
        if sugg["status"] != "pending":
            status_word = "applied ✅" if sugg["status"] == "accepted" else "skipped"
            await query.edit_message_text(
                f"{html.escape(sugg['display'])}\n\n<i>(Already {status_word}.)</i>",
                parse_mode="HTML",
            )
            return

        if action_str == "hbs_reject":
            self.store.update_suggestion_status(suggestion_id, "rejected")
            await query.edit_message_text(
                f"{html.escape(sugg['display'])}\n\n<i>Skipped.</i>",
                parse_mode="HTML",
            )
            return

        # Accept — apply the mechanical change
        habit = sugg["habit"]
        action = sugg["action"]
        val = sugg["value"]
        try:
            if action == "set_cue":
                cue = val.get("cue", "")
                matched = self.store.set_cue_by_name(habit, cue)
                result = (
                    f"✅ Cue set for <b>{html.escape(matched or habit)}</b>: "
                    f"<i>{html.escape(cue)}</i>"
                )
            elif action == "set_days":
                days = val.get("days")
                matched = self.store.set_days_by_name(habit, days)
                days_str = (
                    ", ".join(_INT_TO_ABBR[d] for d in sorted(days))
                    if days
                    else "daily"
                )
                result = f"✅ Schedule updated for <b>{html.escape(matched or habit)}</b>: {days_str}"
            elif action == "rename":
                new_name = val.get("name", "")
                matched = self.store.rename(habit, new_name)
                result = (
                    f"✅ Renamed <b>{html.escape(matched or habit)}</b> → "
                    f"<b>{html.escape(new_name)}</b>"
                )
            elif action == "archive":
                h = self.store._habit_by_name(habit)
                if h:
                    self.store.set_tracked(h["id"], False)
                    result = (
                        f"✅ <b>{html.escape(habit)}</b> archived — won't show in daily "
                        "checks but history is kept."
                    )
                else:
                    result = f"Habit not found: {html.escape(habit)}"
            else:
                result = "Unknown action type."

            self.store.update_suggestion_status(suggestion_id, "accepted")
            await query.edit_message_text(
                f"{html.escape(sugg['display'])}\n\n{result}",
                parse_mode="HTML",
            )
        except Exception as e:
            await query.edit_message_text(
                f"{html.escape(sugg['display'])}\n\n<i>Failed to apply: {html.escape(str(e))}</i>",
                parse_mode="HTML",
            )

    def _manage_message(self) -> tuple[str, InlineKeyboardMarkup]:
        rows = []
        for h in self.store.list_habits(tracked_only=False):
            disp = self.context.habit_display_name(h["name"])
            mark = "" if h["tracked"] else " (off)"
            rows.append(
                [
                    InlineKeyboardButton(f"{disp}{mark}", callback_data="noop"),
                    InlineKeyboardButton(
                        "⏸" if h["tracked"] else "▶️",
                        callback_data=f"hb_{'off' if h['tracked'] else 'on'}:{h['id']}",
                    ),
                    InlineKeyboardButton("🗑", callback_data=f"hb_del:{h['id']}"),
                ]
            )
        return (
            "⚙️ <b>Manage habits</b> — toggle tracking or delete:",
            InlineKeyboardMarkup(rows),
        )

    async def cmd_manage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        if not self.store.list_habits(tracked_only=False):
            await update.message.reply_text(
                "No habits yet. Add one with <code>/addhabit Drink water</code>.",
                parse_mode="HTML",
            )
            return
        text, keyboard = self._manage_message()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def handle_manage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await safe_answer(query)
        action, hid = query.data.split(":", 1)
        habit_id = int(hid)
        if action == "hb_del":
            self.store.remove(habit_id)
        elif action == "hb_off":
            self.store.set_tracked(habit_id, False)
        elif action == "hb_on":
            self.store.set_tracked(habit_id, True)
        text, keyboard = self._manage_message()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

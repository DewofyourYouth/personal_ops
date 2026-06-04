"""Habits — a tracking-domain plugin.

Owns the whole vertical: its own SQLite table (plugins own their schema — the
table is created here, not in core db.py), its data access, the Telegram
checklist + CRUD, and semantic free-text matching.

Source of truth is the `habits` table. On first run it seeds from the legacy
`habits.md`, then projects a read-only `habits.md` back out so the planner's
context bundle (`Context.load_all`) still sees habit scheduling info — the table
is the mutable source, the markdown is a generated view.
"""
from __future__ import annotations

import html
import re

import anthropic
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from context import Context
from habit_tracker import compute_streak, generate_habit_log
from logs import Logs
from tg_common import safe_answer

_HABITS_DDL = """
CREATE TABLE IF NOT EXISTS habits (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    section  TEXT NOT NULL,
    name     TEXT NOT NULL,
    days     TEXT NOT NULL DEFAULT '',   -- CSV of weekday ints (0=Mon..6=Sun); '' = every day
    tracked  INTEGER NOT NULL DEFAULT 1, -- 0 = kept for context only (e.g. "Always off")
    position INTEGER NOT NULL DEFAULT 0
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
        if not self.db.query("SELECT 1 FROM habits LIMIT 1"):
            self._seed_from_markdown()
        self._project_markdown()

    # --- Reads ---

    def list_habits(self, tracked_only: bool = True) -> list[dict]:
        rows = self.db.query("SELECT * FROM habits ORDER BY position, id")
        out = []
        for r in rows:
            if tracked_only and not r["tracked"]:
                continue
            out.append({
                "id": r["id"], "section": r["section"], "name": r["name"],
                "days": _csv_to_days(r["days"]), "tracked": bool(r["tracked"]),
            })
        return out

    def sections(self) -> dict[str, list[dict]]:
        """{section: [habit dicts]} for tracked habits — checklist shape, order preserved."""
        secs: dict[str, list[dict]] = {}
        for h in self.list_habits(tracked_only=True):
            secs.setdefault(h["section"], []).append(h)
        return secs

    # --- Writes (CRUD) ---

    def add(self, name: str, section: str = "Habits", days: list[int] | None = None) -> None:
        nxt = self.db.query("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM habits")[0]["p"]
        self.db.execute(
            "INSERT INTO habits (section, name, days, tracked, position) VALUES (?, ?, ?, 1, ?)",
            (section, name.strip(), _days_to_csv(days), nxt),
        )
        self._project_markdown()

    def remove(self, habit_id: int) -> None:
        self.db.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        self._project_markdown()

    def set_tracked(self, habit_id: int, tracked: bool) -> None:
        self.db.execute("UPDATE habits SET tracked = ? WHERE id = ?", (1 if tracked else 0, habit_id))
        self._project_markdown()

    # --- Seed + projection (table <-> habits.md) ---

    def _seed_from_markdown(self) -> None:
        text = self.context.read("habits.md")
        if not text:
            return
        pos, current, tracked = 0, None, True
        for line in text.splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                tracked = "always off" not in current.lower()
            elif re.match(r"^\s*- ", line) and current is not None:
                raw = re.sub(r"^\s*- ", "", line).strip()
                tag_m = re.search(r"\[([^\]]+)\]$", raw)
                if tag_m:
                    days = [Context._DAY_NAMES[d.strip()] for d in tag_m.group(1).split(",")
                            if d.strip() in Context._DAY_NAMES]
                    name = raw[:tag_m.start()].strip().rstrip("—").strip()
                else:
                    days, name = [], raw
                pos += 1
                self.db.execute(
                    "INSERT INTO habits (section, name, days, tracked, position) VALUES (?, ?, ?, ?, ?)",
                    (current, name, _days_to_csv(days), 1 if tracked else 0, pos),
                )

    def _project_markdown(self) -> None:
        """Regenerate habits.md from the table so Context.load_all (planner context) and
        Obsidian keep working. Source of truth is the table; this file is a derived view."""
        rows = self.db.query("SELECT section, name, days, tracked FROM habits ORDER BY position, id")
        order: list[str] = []
        by_section: dict[str, list] = {}
        for r in rows:
            if r["section"] not in by_section:
                by_section[r["section"]] = []
                order.append(r["section"])
            by_section[r["section"]].append(r)
        lines = ["<!-- generated from the habits table; manage habits via Telegram, not here -->", ""]
        for section in order:
            lines.append(f"## {section}\n")
            for r in by_section[section]:
                days = _csv_to_days(r["days"])
                tag = f" [{','.join(_INT_TO_ABBR[d] for d in sorted(days))}]" if days else ""
                lines.append(f"  - {r['name']}{tag}")
            lines.append("")
        self.context.write("habits.md", "\n".join(lines).rstrip() + "\n")


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
        tools=[{
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
        }],
        tool_choice={"type": "tool", "name": "match_habit"},
        messages=[{"role": "user", "content": f"Habits: {names}\nLog entry: {content!r}\nWhich habit does this satisfy?"}],
    )
    for block in response.content:
        if block.type == "tool_use":
            choice = block.input.get("habit")
            return None if choice in (None, "none") else choice
    return None


class HabitHandlers:
    def __init__(self, bot: Bot, logs: Logs, context: Context, allowed_user: int) -> None:
        self.bot = bot
        self.logs = logs
        self.context = context
        self.allowed_user = allowed_user
        self.store = HabitStore(logs.db, context)   # plugin creates/owns its table here

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("habits", self.cmd_habits))
        app.add_handler(CommandHandler("h", self.cmd_habits))
        app.add_handler(CommandHandler("habitlog", self.cmd_habit_log))
        app.add_handler(CommandHandler("addhabit", self.cmd_add_habit))
        app.add_handler(CommandHandler("managehabits", self.cmd_manage))
        app.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^hb_done:"))
        app.add_handler(CallbackQueryHandler(self.handle_manage, pattern="^hb_(del|on|off):"))

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
        today_weekday = _date.today().weekday()
        sections = self.store.sections()
        logged_today = [e["content"].strip() for e in self.logs.read_today() if e.get("tag") == "habit"]

        all_visible = []
        for habits in sections.values():
            all_visible.extend(h for h in habits if h["days"] is None or today_weekday in h["days"])
        done_ids = set()
        for logged in logged_today:
            h = self._resolve_logged_to_habit(logged, all_visible)
            if h:
                done_ids.add(h["id"])

        lines = ["📋 <b>Habits</b>\n"]
        rows = []
        for section, habits in sections.items():
            visible = [h for h in habits if h["days"] is None or today_weekday in h["days"]]
            if not visible:
                continue
            lines.append(f"<b>{html.escape(section)}</b>")
            for h in visible:
                name = self.context.habit_display_name(h["name"])
                done = h["id"] in done_ids
                lines.append(f"{'✅' if done else '⬜'} {html.escape(name)}")
                if not done:
                    key = name[:52]  # callback_data max 64 bytes; "hb_done:" = 8
                    rows.append([InlineKeyboardButton(f"✅ {name}", callback_data=f"hb_done:{key}")])
            lines.append("")

        return "\n".join(lines).strip(), InlineKeyboardMarkup(rows)

    # --- Handlers: checklist + logging ---

    async def cmd_habits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        if not self.store.list_habits():
            await update.message.reply_text("No habits yet. Add one with <code>/addhabit Drink water</code>.", parse_mode="HTML")
            return
        text, keyboard = self._message()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await safe_answer(query)
        habit_name = query.data.split(":", 1)[1]
        self.logs.write("habit", habit_name)
        text, keyboard = self._message()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def cmd_habit_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        from datetime import date as _date, timedelta as _td
        arg = " ".join(context.args).strip().lower() if context.args else ""
        target = _date.today() - _td(days=1) if arg in ("yesterday", "y") else _date.today()
        template = self.context.dir / "templates" / "habit-template.md"
        output_dir = self.context.dir / "habits"
        try:
            path = generate_habit_log(self.logs, template, output_dir, target)
            await update.message.reply_text(
                f"✅ Habit log saved: <code>{html.escape(path.name)}</code>\nOpen in Obsidian to add notes.",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

    # --- Handlers: CRUD ---

    async def cmd_add_habit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/addhabit <name> [days]  — e.g. /addhabit Stretch [mon,wed,fri]"""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip()
        if not raw:
            await update.message.reply_text("Usage: <code>/addhabit Drink water [mon,wed,fri]</code>", parse_mode="HTML")
            return
        days = None
        tag_m = re.search(r"\[([^\]]+)\]$", raw)
        if tag_m:
            days = [Context._DAY_NAMES[d.strip()] for d in tag_m.group(1).split(",")
                    if d.strip() in Context._DAY_NAMES] or None
            raw = raw[:tag_m.start()].strip()
        self.store.add(raw, days=days)
        await update.message.reply_text(f"➕ Added habit: <b>{html.escape(raw)}</b>", parse_mode="HTML")

    def _manage_message(self) -> tuple[str, InlineKeyboardMarkup]:
        rows = []
        for h in self.store.list_habits(tracked_only=False):
            disp = self.context.habit_display_name(h["name"])
            mark = "" if h["tracked"] else " (off)"
            rows.append([
                InlineKeyboardButton(f"{disp}{mark}", callback_data="noop"),
                InlineKeyboardButton("⏸" if h["tracked"] else "▶️",
                                     callback_data=f"hb_{'off' if h['tracked'] else 'on'}:{h['id']}"),
                InlineKeyboardButton("🗑", callback_data=f"hb_del:{h['id']}"),
            ])
        return "⚙️ <b>Manage habits</b> — toggle tracking or delete:", InlineKeyboardMarkup(rows)

    async def cmd_manage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        if not self.store.list_habits(tracked_only=False):
            await update.message.reply_text("No habits yet. Add one with <code>/addhabit Drink water</code>.", parse_mode="HTML")
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

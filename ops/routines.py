"""Routines — named habit stacks, managed from Telegram.

A routine is an ordered chain of steps with a pulling anchor/deadline (e.g. the
morning routine that ends at the 06:15 chavrusa). Steps are free text; any step that
names a tracked habit is auto-linked at render time so the routine shows that habit's
live streak. The record lives in its own table — the single coherent home for a flow
that would otherwise be scattered across per-habit cue fields.
"""

import html

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from context import Context
from habit_tracker import _matches, compute_streak, load_habit_logs
from logs import Logs

_ROUTINES_DDL = """
CREATE TABLE IF NOT EXISTS routines (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    anchor   TEXT NOT NULL DEFAULT '',  -- the deadline that pulls the chain, e.g. "06:15"
    steps    TEXT NOT NULL DEFAULT '',  -- ordered steps, one per line
    position INTEGER NOT NULL DEFAULT 0
);
"""


class RoutineStore:
    """Routine definitions in SQLite."""

    def __init__(self, db) -> None:
        self.db = db
        self.db.ensure_schema(_ROUTINES_DDL)

    def upsert(self, name: str, steps: list[str], anchor: str = "") -> None:
        """Create or replace a routine by name (replacing is how you reorder/edit)."""
        steps_text = "\n".join(s.strip() for s in steps if s.strip())
        existing = self.get(name)
        if existing:
            self.db.execute(
                "UPDATE routines SET steps = ?, anchor = ? WHERE id = ?",
                (steps_text, anchor.strip(), existing["id"]),
            )
        else:
            nxt = self.db.query(
                "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM routines"
            )[0]["p"]
            self.db.execute(
                "INSERT INTO routines (name, anchor, steps, position) VALUES (?, ?, ?, ?)",
                (name.strip(), anchor.strip(), steps_text, nxt),
            )

    def get(self, name: str) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM routines WHERE LOWER(name) = LOWER(?)", (name.strip(),)
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "name": r["name"],
            "anchor": r["anchor"],
            "steps": [s for s in r["steps"].split("\n") if s],
        }

    def list(self) -> list[dict]:
        return [
            {"name": r["name"], "anchor": r["anchor"]}
            for r in self.db.query("SELECT * FROM routines ORDER BY position, id")
        ]

    def remove(self, name: str) -> bool:
        existing = self.get(name)
        if not existing:
            return False
        self.db.execute("DELETE FROM routines WHERE id = ?", (existing["id"],))
        return True

    def insert_step(self, name: str, position: int, step: str) -> list[str] | None:
        """Insert `step` at 1-based `position` (clamped); position past the end appends.
        Returns the new step list, or None if the routine doesn't exist."""
        routine = self.get(name)
        if not routine:
            return None
        steps = routine["steps"]
        idx = max(0, min(position - 1, len(steps)))
        steps.insert(idx, step.strip())
        self.upsert(routine["name"], steps, routine["anchor"])
        return steps

    def remove_step(self, name: str, position: int) -> str | None:
        """Remove the step at 1-based `position`. Returns the removed step text, or None."""
        routine = self.get(name)
        if not routine or not (1 <= position <= len(routine["steps"])):
            return None
        removed = routine["steps"].pop(position - 1)
        self.upsert(routine["name"], routine["steps"], routine["anchor"])
        return removed


class RoutineHandlers:
    def __init__(
        self, bot: Bot, logs: Logs, context: Context, allowed_user: int
    ) -> None:
        self.bot = bot
        self.logs = logs
        self.context = context
        self.allowed_user = allowed_user
        self.store = RoutineStore(logs.db)

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("routines", self.cmd_routines))
        app.add_handler(CommandHandler("routine", self.cmd_routine))
        app.add_handler(CommandHandler("addroutine", self.cmd_add_routine))
        app.add_handler(CommandHandler("delroutine", self.cmd_del_routine))
        app.add_handler(CommandHandler("routinestep", self.cmd_routine_step))

    # --- Habit linking ---

    def _streak_for_step(self, step: str, habits: list, logged_by_day: dict) -> str:
        """If a step names a tracked habit, return its ' 🔥N' badge, else ''."""
        for h in habits:
            display = self.context.habit_display_name(h["name"])
            if _matches(display, step) or _matches(h["name"], step):
                cur, _ = compute_streak(
                    self.logs,
                    h["name"],
                    due_weekdays=h["days"],
                    logged_by_day=logged_by_day,
                )
                return f"  🔥{cur}" if cur else "  🔗"
        return ""

    def _tracked_habits(self) -> list[dict]:
        rows = self.logs.db.query("SELECT name, days FROM habits WHERE tracked = 1")
        return [
            {
                "name": r["name"],
                "days": [int(d) for d in r["days"].split(",") if d != ""] or None,
            }
            for r in rows
        ]

    def _render(self, routine: dict) -> str:
        habits = self._tracked_habits()
        logged_by_day = load_habit_logs(self.logs)
        anchor = (
            f"  <i>(by {html.escape(routine['anchor'])})</i>"
            if routine["anchor"]
            else ""
        )
        lines = [f"🌅 <b>{html.escape(routine['name'])}</b>{anchor}\n"]
        for i, step in enumerate(routine["steps"], 1):
            badge = self._streak_for_step(step, habits, logged_by_day)
            lines.append(f"{i}. {html.escape(step)}{badge}")
        return "\n".join(lines)

    # --- Commands ---

    async def cmd_routines(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        routines = self.store.list()
        if not routines:
            await update.message.reply_text(
                "No routines yet. Create one:\n"
                "<code>/addroutine Morning @06:15: 22:30 lights out | 5:30 wake | "
                "strength | coffee | shul</code>",
                parse_mode="HTML",
            )
            return
        lines = ["🗂 <b>Routines</b>\n"]
        for r in routines:
            anchor = f" (by {r['anchor']})" if r["anchor"] else ""
            lines.append(f"• {html.escape(r['name'])}{anchor}")
        lines.append("\n<code>/routine &lt;name&gt;</code> to view one.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_routine(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        name = " ".join(context.args).strip() if context.args else ""
        if not name:
            await self.cmd_routines(update, context)
            return
        routine = self.store.get(name)
        if not routine:
            await update.message.reply_text(
                f"No routine “{html.escape(name)}”. /routines to list them."
            )
            return
        await update.message.reply_text(self._render(routine), parse_mode="HTML")

    async def cmd_add_routine(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/addroutine <name> [@anchor]: step | step | step  (replaces if name exists)."""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        if ":" not in raw:
            await update.message.reply_text(
                "Usage: <code>/addroutine Morning @06:15: 22:30 lights out | 5:30 wake | "
                "strength | coffee | shul</code>\n(steps separated by | ; @time is the "
                "deadline). Re-run with the full list to reorder.",
                parse_mode="HTML",
            )
            return
        head, steps_part = raw.split(":", 1)
        anchor = ""
        if "@" in head:
            head, anchor = head.split("@", 1)
        name = head.strip()
        steps = [s.strip() for s in steps_part.split("|") if s.strip()]
        if not name or not steps:
            await update.message.reply_text("Need a name and at least one step.")
            return
        self.store.upsert(name, steps, anchor.strip())
        await update.message.reply_text(
            self._render(self.store.get(name)), parse_mode="HTML"
        )

    async def cmd_routine_step(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Edit one step without retyping the whole routine.
        /routinestep Morning: add 3 weigh myself   — insert at position 3
        /routinestep Morning: rm 3                  — remove step 3
        (positions are the numbers shown by /routine)"""
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        usage = (
            "Usage:\n<code>/routinestep Morning: add 3 weigh myself</code>\n"
            "<code>/routinestep Morning: rm 3</code>\n(positions are the numbers in /routine)"
        )
        if ":" not in raw:
            await update.message.reply_text(usage, parse_mode="HTML")
            return
        name, rest = raw.split(":", 1)
        parts = rest.split()
        if len(parts) < 2 or parts[0] not in ("add", "rm") or not parts[1].isdigit():
            await update.message.reply_text(usage, parse_mode="HTML")
            return
        verb, pos = parts[0], int(parts[1])

        if verb == "add":
            step = rest.split(None, 2)[2] if len(parts) >= 3 else ""
            if not step:
                await update.message.reply_text("Add what? Give the step text.")
                return
            if self.store.insert_step(name.strip(), pos, step) is None:
                await update.message.reply_text(f"No routine “{html.escape(name.strip())}”.")
                return
        else:  # rm
            removed = self.store.remove_step(name.strip(), pos)
            if removed is None:
                await update.message.reply_text(
                    f"No step {pos} in “{html.escape(name.strip())}” (or no such routine)."
                )
                return
        await update.message.reply_text(
            self._render(self.store.get(name.strip())), parse_mode="HTML"
        )

    async def cmd_del_routine(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        name = " ".join(context.args).strip() if context.args else ""
        if self.store.remove(name):
            await update.message.reply_text(f"🗑 Removed routine “{html.escape(name)}”.")
        else:
            await update.message.reply_text(f"No routine “{html.escape(name)}”.")

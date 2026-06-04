"""Habits — a tracking-domain plugin.

Feature class (same shape as the core handlers): built with the bot + the
services it needs, handlers are methods, self-registers via `register(app)`.
Satisfies the `Trackable` capability (structurally) via `summary(days)`.

Habit definitions live in the Obsidian vault (`habits.md`, read through Context);
a habit is "done" today if a matching `habit`-tagged log entry exists.
"""
import html

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from context import Context
from habit_tracker import compute_streak, generate_habit_log
from logs import Logs
from tg_common import safe_answer


class HabitHandlers:
    def __init__(self, bot: Bot, logs: Logs, context: Context, allowed_user: int) -> None:
        self.bot = bot
        self.logs = logs
        self.context = context
        self.allowed_user = allowed_user

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("habits", self.cmd_habits))
        app.add_handler(CommandHandler("h", self.cmd_habits))
        app.add_handler(CommandHandler("habitlog", self.cmd_habit_log))
        app.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^hb_done:"))

    # --- Trackable capability ---

    def summary(self, days: int) -> str:
        """Current habit streaks within the window — for the digest / eval harness."""
        sections = self.context.parse_habits()
        names = [h["text"] for habits in sections.values() for h in habits]
        if not names:
            return ""
        parts = []
        for name in names:
            current, _ = compute_streak(self.logs, name, lookback=max(days, 1))
            disp = self.context.habit_display_name(name)
            parts.append(f"{disp} 🔥{current}" if current else f"{disp} —")
        return f"Habit streaks (last {days}d): " + ", ".join(parts)

    # --- Helpers ---

    def _resolve_logged_to_habit(self, logged: str, all_habits: list) -> dict | None:
        """Map a logged entry to its habit by exact display-name match.

        Both write paths now store the canonical habit name — button taps write it
        directly, and free-text logs are resolved to it semantically at log time
        (`llm.match_habit`). So an exact match is all the checklist needs; the old
        fuzzy word-overlap + stopword heuristic is gone.
        """
        logged_l = logged.strip().lower()
        for h in all_habits:
            if logged_l == self.context.habit_display_name(h["text"]).strip().lower():
                return h
        return None

    def _message(self) -> tuple[str, InlineKeyboardMarkup]:
        from datetime import date as _date
        today_weekday = _date.today().weekday()
        sections = self.context.parse_habits()
        logged_today = [e["content"].strip() for e in self.logs.read_today() if e.get("tag") == "habit"]

        # Flat list of all habits visible today, then resolve each log to exactly one of them
        all_visible = []
        for habits in sections.values():
            all_visible.extend(h for h in habits if h["days"] is None or today_weekday in h["days"])
        done_keys = set()
        for logged in logged_today:
            h = self._resolve_logged_to_habit(logged, all_visible)
            if h:
                done_keys.add(h["raw"])

        lines = ["📋 <b>Habits</b>\n"]
        rows = []
        for section, habits in sections.items():
            visible = [h for h in habits if h["days"] is None or today_weekday in h["days"]]
            if not visible:
                continue
            lines.append(f"<b>{html.escape(section)}</b>")
            for h in visible:
                name = self.context.habit_display_name(h["text"])
                done = h["raw"] in done_keys
                lines.append(f"{'✅' if done else '⬜'} {html.escape(name)}")
                if not done:
                    key = name[:52]  # callback_data max 64 bytes; "hb_done:" = 8
                    rows.append([InlineKeyboardButton(f"✅ {name}", callback_data=f"hb_done:{key}")])
            lines.append("")

        return "\n".join(lines).strip(), InlineKeyboardMarkup(rows)

    # --- Handlers ---

    async def cmd_habits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        if not self.context.parse_habits():
            await update.message.reply_text("No habits defined. Edit habits.md in your vault.")
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
            # Wrap the filename in <code> so Telegram doesn't auto-link the .md (a real TLD)
            # into a broken web URL. Code spans are also long-press-to-copy.
            await update.message.reply_text(
                f"✅ Habit log saved: <code>{html.escape(path.name)}</code>\nOpen in Obsidian to add notes.",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

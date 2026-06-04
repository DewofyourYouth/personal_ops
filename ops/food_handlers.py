"""Food — a tracking-domain plugin.

Feature class: built with the bot + logs, `/food` is a method, self-registers via
`register(app)`, and satisfies `Trackable` via `summary(days)`.

Food is logged with the `food:` / `ate:` prefixes (the dispatcher enriches the
entry with parsed macros at write time); this plugin owns the read side.
"""
import html
from datetime import date, timedelta

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from logs import Logs


class FoodHandlers:
    def __init__(self, bot: Bot, logs: Logs, allowed_user: int) -> None:
        self.bot = bot
        self.logs = logs
        self.allowed_user = allowed_user

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("food", self.cmd_food))

    # --- Trackable capability ---

    def summary(self, days: int) -> str:
        """How consistently food was logged over the window — for the digest / eval."""
        start = date.today() - timedelta(days=max(days, 1) - 1)
        rows = self.logs.db.entries_for_range(start, date.today())
        food = [r for r in rows if r["tag"] == "food"]
        if not food:
            return ""
        days_with = len({r["date"] for r in food})
        return f"Food: {len(food)} entries logged on {days_with}/{days} days."

    # --- Handlers ---

    async def cmd_food(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        entries = [e for e in self.logs.read_today() if e.get("tag") == "food"]
        if not entries:
            await update.message.reply_text(
                "Nothing logged yet today. Use <code>food: what you ate</code>.", parse_mode="HTML"
            )
            return
        lines = ["🍽 <b>Today's food log:</b>\n"]
        for e in entries:
            t = e["ts"][11:16]
            lines.append(f"<code>{t}</code> {html.escape(e['content'])}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

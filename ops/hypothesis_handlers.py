"""Hypothesis feature — the /hypotheses list, resolve buttons, and the follow-up job.

A feature class (same shape as the other handlers): built with the bot and the
Hypotheses service; commands and callbacks are methods that self-register via
`register(app)`.

Creating a hypothesis lives in the text router (it's part of the prefix flow).
This module owns everything after: listing open tests, resolving them, and the
daily follow-up that pulls the metric readings logged since each test was raised.
`run_followups` is wrapped by a thin module-level function in bot.py so the
persistent job store gets a picklable callable.
"""

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from hypotheses import Hypotheses
from tg_common import safe_answer

_STATUS_ICON = {
    "active": "🔬",
    "prompted": "⏳",
    "confirmed": "✅",
    "falsified": "❌",
    "dropped": "🗑",
}


def _resolve_keyboard(hyp_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirmed", callback_data=f"hyp_confirmed_{hyp_id}"
                ),
                InlineKeyboardButton(
                    "❌ Falsified", callback_data=f"hyp_falsified_{hyp_id}"
                ),
            ],
            [InlineKeyboardButton("🗑 Drop", callback_data=f"hyp_dropped_{hyp_id}")],
        ]
    )


class HypothesisHandlers:
    def __init__(self, bot, hypotheses: Hypotheses, allowed_user: int) -> None:
        self.bot = bot
        self.hypotheses = hypotheses
        self.allowed_user = allowed_user

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("hypotheses", self.cmd_list))
        app.add_handler(CallbackQueryHandler(self.handle_resolve, pattern="^hyp_"))

    async def cmd_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        rows = self.hypotheses.open()
        if not rows:
            await update.message.reply_text(
                "No open hypotheses. Log one with `hypothesis: …`."
            )
            return
        for r in rows:
            icon = _STATUS_ICON.get(r["status"], "🔬")
            text = html.escape(r["restatement"] or r["text"])
            body = f"{icon} <b>{text}</b>\n<i>raised {r['created']}"
            if r["follow_up_date"]:
                body += f" · follow-up {r['follow_up_date']}"
            body += "</i>"
            await update.message.reply_text(
                body, parse_mode="HTML", reply_markup=_resolve_keyboard(r["id"])
            )

    async def handle_resolve(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await safe_answer(query)
        # callback_data: hyp_<status>_<id>
        _, status, hyp_id = query.data.split("_", 2)
        self.hypotheses.set_status(int(hyp_id), status)
        icon = _STATUS_ICON.get(status, "•")
        await query.edit_message_text(
            f"{icon} Marked <b>{status}</b>.", parse_mode="HTML"
        )

    # --- Scheduled follow-up (wrapped by bot.py for the scheduler) ---

    async def run_followups(self) -> None:
        """Send the check-in for any hypothesis whose follow-up date has arrived,
        with the metric readings logged since it was raised. Mark it prompted so it
        fires once, not every day."""
        for r in self.hypotheses.due():
            await self.bot.send_message(
                chat_id=self.allowed_user,
                text=self.hypotheses.followup_report(r),
                parse_mode="HTML",
                reply_markup=_resolve_keyboard(r["id"]),
            )
            self.hypotheses.set_status(r["id"], "prompted")

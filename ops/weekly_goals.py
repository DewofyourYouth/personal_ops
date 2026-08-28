"""Time-bounded weekly focus goals — distinct from the static long-term
ops/context/goals.md. A goal stated conversationally ("this week I want to
focus on X") shapes the daily agenda proposal while it's active, and gets
reviewed (not silently expired) at the Sunday clearing session: a message
listing every active goal with a Delete button and a Roll Over button.

WeeklyGoals is the domain service (JSON CRUD, no Telegram concerns), mirroring
Backlog's single-persistent-file model but with Agenda's status-based CRUD
shape. WeeklyGoalsHandlers is the Telegram-facing half — the clearing-session
job body and the Delete/Roll Over button handler — mirroring how HabitStore
and HabitHandlers live side by side in habit_handlers.py.
"""

import html
import json
import logging
import os
import uuid
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from tg_common import safe_answer

logger = logging.getLogger(__name__)


def _week_start(d: date) -> date:
    """The Sunday starting the week `d` falls in (Sunday itself if `d` is a
    Sunday). Python's weekday() is Monday=0..Sunday=6."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


class WeeklyGoals:
    def __init__(self, log_dir: str):
        self.path = os.path.join(log_dir, "weekly_goals.json")

    def load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return []

    def save(self, items: list[dict]) -> None:
        with open(self.path, "w") as f:
            json.dump(items, f, indent=2)

    def add(self, text: str) -> dict:
        items = self.load()
        today = date.today()
        entry = {
            "id": str(uuid.uuid4())[:8],
            "text": text.strip(),
            "created": today.isoformat(),
            "week_of": _week_start(today).isoformat(),
            "status": "active",
        }
        items.append(entry)
        self.save(items)
        return entry

    def active(self) -> list[dict]:
        """Every currently-active goal, regardless of which week it was
        created in — the clearing session reviews everything still active,
        not just items from the literal current week."""
        return [i for i in self.load() if i["status"] == "active"]

    def get(self, item_id: str) -> dict | None:
        return next((i for i in self.load() if i["id"] == item_id), None)

    def delete(self, item_id: str) -> bool:
        """Soft delete — marks the item, doesn't remove it, so the clearing
        session's idempotency guard can tell 'already handled' from 'unknown
        id' the same way handle_suggestion does for habit suggestions."""
        items = self.load()
        for item in items:
            if item["id"] == item_id and item["status"] == "active":
                item["status"] = "deleted"
                self.save(items)
                return True
        return False

    def roll_over(self, item_id: str) -> bool:
        """Bump week_of to next Sunday; stays active."""
        items = self.load()
        for item in items:
            if item["id"] == item_id and item["status"] == "active":
                current = date.fromisoformat(item["week_of"])
                item["week_of"] = (current + timedelta(days=7)).isoformat()
                self.save(items)
                return True
        return False

    def format_for_prompt(self) -> str:
        """Render active goals as a markdown block for the agenda-proposal
        prompt, or '' if there are none — same shape as
        Baseline.format_for_prompt()."""
        items = self.active()
        if not items:
            return ""
        lines = ["## This week's focus\n"]
        lines.extend(f"- {i['text']}" for i in items)
        return "\n".join(lines)


class WeeklyGoalsHandlers:
    def __init__(self, bot, weekly_goals: WeeklyGoals, allowed_user: int) -> None:
        self.bot = bot
        self.store = weekly_goals
        self.allowed_user = allowed_user

    def register(self, app: Application) -> None:
        app.add_handler(
            CallbackQueryHandler(
                self.handle_goal_review, pattern="^wg_(delete|rollover):"
            )
        )

    async def send_weekly_review(self) -> None:
        """Sunday clearing session — one message per active goal with a
        Delete/Roll Over button pair. Sends nothing if there's nothing active,
        same 'nothing to do' quiet-skip as the retrain job."""
        items = self.store.active()
        if not items:
            return
        for item in items:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Delete", callback_data=f"wg_delete:{item['id']}"
                        ),
                        InlineKeyboardButton(
                            "🔁 Roll Over", callback_data=f"wg_rollover:{item['id']}"
                        ),
                    ]
                ]
            )
            await self.bot.send_message(
                chat_id=self.allowed_user,
                text=f"🎯 <b>Weekly focus review</b>\n\n<blockquote>{html.escape(item['text'])}</blockquote>",
                parse_mode="HTML",
                reply_markup=keyboard,
            )

    async def handle_goal_review(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        assert update.callback_query is not None
        query = update.callback_query
        await safe_answer(query)
        assert query.data is not None
        action_str, item_id = query.data.split(":", 1)
        item = self.store.get(item_id)
        if item is None or item["status"] != "active":
            await query.edit_message_text("Already handled.")
            return
        try:
            if action_str == "wg_delete":
                self.store.delete(item_id)
                result = f"🗑 Removed: {html.escape(item['text'])}"
            else:
                self.store.roll_over(item_id)
                result = f"🔁 Rolled over: {html.escape(item['text'])}"
            await query.edit_message_text(result, parse_mode="HTML")
        except Exception as e:
            await query.edit_message_text(f"Failed: {html.escape(str(e))}")

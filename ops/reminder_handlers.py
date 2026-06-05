"""Reminders feature — list / edit / delete UI plus the due-reminder firing job.

A feature class (same shape as the other handlers): built with the bot and the
domain services it needs; its commands and callbacks are methods that
self-register via `register(app)`. The edit-reply conversation state lives on the
instance.

Creating reminders from free text ("remind me …") lives in the text router —
it's part of `process_text`. This module owns everything after creation:
listing, editing, deleting, and firing due reminders. The scheduled firing
(`run_due_check`) is wrapped by a thin module-level function in bot.py, because
the persistent job store needs a picklable callable (a bound method holding the
Bot isn't picklable).
"""

import html
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from logs import Logs
from reminders import Reminders
from text_router import _parse_time
from tg_common import safe_answer

_TZ = ZoneInfo("Asia/Jerusalem")


def _label(r: dict) -> str:
    text = r["text"]
    short = text if len(text) <= 45 else text[:44] + "…"
    if r["type"] == "once":
        return f"⏰ {short} — {r.get('date', 'today')} {r.get('time', '?')}"
    elif r["type"] == "daily":
        return f"⏰ {short} — daily {r['time']}"
    elif r["type"] == "weekly":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_name = days[r.get("day", 4)]
        return f"⏰ {short} — every {day_name} {r['time']}"
    else:
        return f"⏰ {short} — every {r['interval_minutes']}m"


def _next_occurrence(reminder: dict) -> datetime:
    """Return the next datetime this reminder will fire, for sorting purposes."""
    now = datetime.now(_TZ)
    today = now.date()
    far_future = datetime(9999, 12, 31, tzinfo=_TZ)

    def parse_hours_and_minutes(r: dict):
        return map(int, r.get("time", "23:59").split(":"))

    try:
        match reminder["type"]:
            case "once":
                d = date.fromisoformat(reminder.get("date", "9999-12-31"))
                h, m = parse_hours_and_minutes(reminder)
                return datetime(d.year, d.month, d.day, h, m, tzinfo=_TZ)
            case "daily":
                h, m = parse_hours_and_minutes(reminder)
                candidate = datetime(
                    today.year, today.month, today.day, h, m, tzinfo=_TZ
                )
                if candidate <= now:
                    candidate += timedelta(days=1)
                return candidate
            case "weekly":
                h, m = parse_hours_and_minutes(reminder)
                target_day = reminder.get("day", 0)
                days_ahead = (target_day - now.weekday()) % 7 or 7
                next_date = today + timedelta(days=days_ahead)
                candidate = datetime(
                    next_date.year, next_date.month, next_date.day, h, m, tzinfo=_TZ
                )
                if candidate <= now:
                    next_date += timedelta(days=7)
                    candidate = datetime(
                        next_date.year, next_date.month, next_date.day, h, m, tzinfo=_TZ
                    )
                return candidate
            case "interval":
                interval = reminder.get("interval_minutes", 60)
                start_h, start_m = map(
                    int, reminder.get("window_start", "08:00").split(":")
                )
                window_start = datetime(
                    today.year, today.month, today.day, start_h, start_m, tzinfo=_TZ
                )
                current_minutes = now.hour * 60 + now.minute
                start_minutes = start_h * 60 + start_m
                elapsed = current_minutes - start_minutes
                if elapsed < 0:
                    return window_start
                next_tick = start_minutes + (elapsed // interval + 1) * interval
                next_h, next_m = divmod(next_tick, 60)
                return datetime(
                    today.year, today.month, today.day, next_h, next_m, tzinfo=_TZ
                )
    except Exception:
        pass
    # Unknown type or a parse error: sort it last rather than crashing the sort.
    return far_future


def _keyboard(all_reminders: list) -> InlineKeyboardMarkup:
    rows = []
    for r in sorted(all_reminders, key=_next_occurrence):
        # Full-width label row so the whole reminder + time is visible, with the
        # edit/delete actions on their own row beneath it.
        rows.append([InlineKeyboardButton(_label(r), callback_data="noop")])
        rows.append(
            [
                InlineKeyboardButton("✏️ Edit", callback_data=f"rm_edit:{r['id']}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"rm_del:{r['id']}"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def _apply_edit(r: dict, instruction: str) -> str:
    """Mutate reminder r per a free-text instruction. Returns a human summary of what changed."""
    instr = instruction.strip().lower()
    # Relative shift: "30 minutes earlier", "an hour later", "15 min earlier"
    shift = re.search(
        r"(\d+|an?|a)\s*(hour|hr|minute|min)s?\s*(earlier|later|before|after|sooner)",
        instr,
    )
    if shift and r.get("time"):
        qty = 1 if shift.group(1) in ("a", "an") else int(shift.group(1))
        mins = qty * (60 if shift.group(2).startswith(("hour", "hr")) else 1)
        if shift.group(3) in ("earlier", "before", "sooner"):
            mins = -mins
        h, m = map(int, r["time"].split(":"))
        total = (h * 60 + m + mins) % (24 * 60)
        r["time"] = f"{total // 60:02d}:{total % 60:02d}"
        return f"time → {r['time']}"
    # Absolute new time
    t = _parse_time(instruction)
    if t:
        r["time"] = t
        return f"time → {t}"
    # Otherwise treat as new text
    r["text"] = instruction.strip()
    return f"text → {r['text']}"


class ReminderHandlers:
    def __init__(
        self, bot: Bot, reminders: Reminders, logs: Logs, shabbat, allowed_user: int
    ) -> None:
        self.bot = bot
        self.reminders = reminders
        self.logs = logs
        self.shabbat = shabbat
        self.allowed_user = allowed_user
        self._awaiting_edit: dict = {}  # chat_id -> reminder id being edited

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("reminders", self.cmd_reminders))
        app.add_handler(CommandHandler("r", self.cmd_reminders))
        app.add_handler(CallbackQueryHandler(self.handle_delete, pattern="^rm_del:"))
        app.add_handler(CallbackQueryHandler(self.handle_edit, pattern="^rm_edit:"))

    # --- Commands + callbacks ---

    async def cmd_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        all_reminders = self.reminders.load()
        if not all_reminders:
            await update.message.reply_text(
                "No reminders set. Use 'remind me...' to add one."
            )
            return
        await update.message.reply_text(
            "⏰ <b>Reminders:</b>",
            parse_mode="HTML",
            reply_markup=_keyboard(all_reminders),
        )

    async def handle_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await safe_answer(query)
        reminder_id = query.data.split(":")[1]
        self.reminders.remove(reminder_id)
        all_reminders = self.reminders.load()
        if not all_reminders:
            await query.edit_message_text("All reminders removed.")
            return
        await query.edit_message_reply_markup(reply_markup=_keyboard(all_reminders))

    async def handle_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await safe_answer(query)
        reminder_id = query.data.split(":")[1]
        r = next((x for x in self.reminders.load() if x["id"] == reminder_id), None)
        if not r:
            await query.edit_message_text("That reminder no longer exists.")
            return
        self._awaiting_edit[query.message.chat_id] = reminder_id
        cur = r.get("time", "—")
        await query.edit_message_text(
            f"✏️ Editing: <i>{html.escape(r['text'])}</i> (currently {cur}).\n\n"
            "Send a change: a new time (<code>18:00</code>), a shift "
            "(<code>30 minutes earlier</code>, <code>an hour later</code>), or new text.",
            parse_mode="HTML",
        )

    async def try_handle_edit_reply(self, update: Update) -> bool:
        """If a reminder edit is pending, apply this reply. Returns True if it
        consumed the message."""
        chat_id = update.effective_chat.id
        if chat_id not in self._awaiting_edit:
            return False
        rid = self._awaiting_edit.pop(chat_id)
        text = update.message.text
        if text.strip().lower() == "/cancel":
            await update.message.reply_text("Edit cancelled.")
            return True
        all_r = self.reminders.load()
        r = next((x for x in all_r if x["id"] == rid), None)
        if not r:
            await update.message.reply_text("That reminder no longer exists.")
            return True
        summary = _apply_edit(r, text)
        self.reminders.save(all_r)
        await update.message.reply_text(
            f"✏️ Updated: <i>{html.escape(r['text'])}</i> — {summary}", parse_mode="HTML"
        )
        return True

    # --- Scheduled firing (wrapped by bot.py for the scheduler) ---

    async def run_due_check(self) -> None:
        if self.shabbat.quiet_now():
            return
        for r in self.reminders.due_now():
            if r.get("auto_log"):
                self.logs.write("reminder", r["text"])
            is_checkin = any(
                w in r["text"].lower() for w in ("check in", "checkin", "check-in")
            )
            cb = "remind_dismiss_c" if is_checkin else "remind_dismiss"
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✓ Dismiss", callback_data=cb)]]
            )
            await self.bot.send_message(
                chat_id=self.allowed_user,
                text=f"⏰ <b>{html.escape(r['text'])}</b>",
                parse_mode="HTML",
                reply_markup=keyboard,
            )

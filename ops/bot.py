import asyncio
import difflib
import html
import json
import os
import random
import re
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import openai
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agenda import Agenda
from context import Context
from gcal import GCal
from logs import Logs
from planner import Planner, day_type
from agenda_queue import AgendaQueue
from backlog import Backlog
from reminders import Reminders
from baseline_tracker import Baseline

TOKEN = os.environ["OPS_BOT_TOKEN"]
ALLOWED_USER = int(os.environ["OPS_CHAT_ID"])
MODEL = os.environ.get("OPS_MODEL", "claude-haiku-4-5-20251001")
PLAN_HOUR = int(os.environ.get("OPS_PLAN_HOUR", "8"))
PLAN_MINUTE = int(os.environ.get("OPS_PLAN_MINUTE", "0"))

cwd = os.getcwd()
LOG_DIR = os.path.expanduser(f"{cwd}/ops/log")

# Global bot reference — set in post_init once the Application starts
_bot = None

# APScheduler with SQLite job store so jobs survive restarts
_scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{LOG_DIR}/scheduler.db")},
    timezone=ZoneInfo("Asia/Jerusalem"),
)

# --- Service instances ---
logs      = Logs(LOG_DIR)
agenda_   = Agenda(LOG_DIR)
queue_    = AgendaQueue(LOG_DIR)
backlog_  = Backlog(LOG_DIR)
reminders = Reminders()
gcal_     = GCal()
context_  = Context()
planner_  = Planner(MODEL, logs, context_)
baseline_ = Baseline(LOG_DIR)

PREFIXES = {
    "insight:":    "#insight",
    "hypothesis:": "#hypothesis",
    "checkin":     "#checkin",
    "task:":       "#task",
    "note:":       "#note",
    "did:":        "#win",
    "habit:":      "#habit",
    "wrong:":      "#wrong",
    "backlog:":    "#backlog",
    "someday:":    "#backlog",
    "food:":       "#food",
    "ate:":        "#food",
    "ate ":        "#food",
    "skip:":       "#skip",
    "excuse:":     "#skip",
    "excused:":    "#skip",
}

# Matches "feedback:", "feedback request", "question:", "I have a question", etc.
_FEEDBACK_RE = re.compile(
    r"^(?:feedback(?:\s+request)?|question|i\s+have\s+a\s+(?:question|thought)|i\s+want\s+(?:feedback|your\s+take))"
    r"(?:[,:.\s\-]+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)

# Matches "checkin", "checking in", "check in", "update", "status update", etc.
_CHECKIN_RE = re.compile(
    r"^(?:check(?:ing|in)?(?:\s+in)?|update|status(?:\s+update)?)"
    r"(?:[,:.\s\-]+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)

STATUS_ICONS = {
    "open": "⌛",
    "done": "✅",
    "missed": "❌",
}

# Pending proposal state keyed by chat_id (single-user bot, in-memory is fine)
_pending: dict = {}
_awaiting_time: dict = {}        # chat_id -> partial reminder dict waiting for a time reply
_awaiting_context: dict = {}     # chat_id -> filename waiting for new content
_awaiting_job: dict = {}         # chat_id -> {"step": str, "data": dict}
_awaiting_candles: dict = {}     # chat_id -> True
_awaiting_voice_edit: dict = {}  # chat_id -> pending transcript text

_ENCOURAGEMENTS = [
    "Look at you, a functioning adult!",
    "Your future self just breathed a sigh of relief.",
    "Scientists confirm: doing things is better than not doing things. 🧪",
    "Task defeated 🤺. It never stood a chance.",
    "You absolute legend. Probably.",
    "Gold star 🌟. Imaginary, but still.",
    "This is going straight to your permanent record. The good one. 📓",
    "Somewhere a productivity guru is shedding a single tear of joy. 🥲",
    "Your mom would be proud. Assuming she cares about task management.",
    "That task is dead. You killed it. No regrets. 🪦",
    "Wow. Just... wow. (Keep going.)",
    "The dopamine was real. Ride it. 👊",
]

def _encourage() -> str:
    return random.choice(_ENCOURAGEMENTS)


async def _safe_answer(query, text: str = "") -> None:
    try:
        await query.answer(text)
    except BadRequest:
        pass  # query expired (bot restarted, old button tapped)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (NetworkError, BadRequest)):
        return  # transient — swallow silently
    raise context.error


def _reminded_path():
    from datetime import date
    return os.path.join(LOG_DIR, f"{date.today()}-reminded.txt")


def _load_reminded() -> set:
    path = _reminded_path()
    if not os.path.exists(path):
        return set()
    return set(open(path).read().splitlines())


def _save_reminded(eid: str):
    with open(_reminded_path(), "a") as f:
        f.write(eid + "\n")


# --- Proposal UI helpers ---

def _proposal_keyboard(items: list[str], selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for i, item in enumerate(items):
        mark = "✅" if i in selected else "⬜"
        label = item if len(item) <= 32 else item[:29] + "…"
        rows.append([
            InlineKeyboardButton(f"{mark} {i + 1}. {label}", callback_data=f"pt_t:{i}"),
            InlineKeyboardButton("✏️", callback_data=f"pt_e:{i}"),
        ])
    rows.append([
        InlineKeyboardButton("Confirm", callback_data="pt_ok"),
        InlineKeyboardButton("Accept All", callback_data="pt_all"),
        InlineKeyboardButton("Cancel", callback_data="pt_no"),
    ])
    return InlineKeyboardMarkup(rows)


def _proposal_text(items: list[str], selected: set[int]) -> str:
    lines = [f"📋 <b>Proposed agenda ({html.escape(day_type())}):</b>\n"]
    for i, item in enumerate(items):
        mark = "✅" if i in selected else "⬜"
        lines.append(f"{mark} {i + 1}. {html.escape(item)}")
    lines.append("\n<i>Tap items to toggle, then Confirm.</i>")
    return "\n".join(lines)


# --- Command handlers ---

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text("Generating today's agenda…")
    await _send_proposal(update.effective_chat.id)


def _reminder_label(r: dict) -> str:
    text = r["text"]
    short = text if len(text) <= 20 else text[:17] + "…"
    if r["type"] == "once":
        return f"⏰ {short} — {r.get('date', 'today')} {r.get('time', '?')}"
    elif r["type"] == "daily":
        return f"⏰ {short} — daily {r['time']}"
    elif r["type"] == "weekly":
        days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        day_name = days[r.get("day", 4)]
        return f"⏰ {short} — every {day_name} {r['time']}"
    else:
        return f"⏰ {short} — every {r['interval_minutes']}m"


def _reminder_sort_key(r: dict) -> tuple:
    if r["type"] == "once":
        return (0, r.get("date", "9999-99-99"), r.get("time", "99:99"))
    elif r["type"] == "daily":
        return (1, "", r.get("time", "99:99"))
    else:
        return (2, "", r.get("window_start", "99:99"))


def _reminders_keyboard(all_reminders: list) -> InlineKeyboardMarkup:
    rows = []
    for r in sorted(all_reminders, key=_reminder_sort_key):
        rows.append([
            InlineKeyboardButton(_reminder_label(r), callback_data="noop"),
            InlineKeyboardButton("🗑", callback_data=f"rm_del:{r['id']}"),
        ])
    return InlineKeyboardMarkup(rows)


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    all_reminders = reminders.load()
    if not all_reminders:
        await update.message.reply_text("No reminders set. Use 'remind me...' to add one.")
        return
    await update.message.reply_text(
        "⏰ <b>Reminders:</b>", parse_mode="HTML",
        reply_markup=_reminders_keyboard(all_reminders)
    )


async def handle_reminder_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    reminder_id = query.data.split(":")[1]
    reminders.remove(reminder_id)
    all_reminders = reminders.load()
    if not all_reminders:
        await query.edit_message_text("All reminders removed.")
        return
    await query.edit_message_reply_markup(reply_markup=_reminders_keyboard(all_reminders))


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    try:
        events = await asyncio.to_thread(gcal_.get_today_events)
        text = gcal_.format_events(events)
    except Exception as e:
        text = f"Could not fetch calendar: {e}"
    await update.message.reply_text(f"📅 <b>Today's events:</b>\n{html.escape(text)}", parse_mode="HTML")


def _agenda_message(open_items: list) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["📋 <b>Open items:</b>\n"]
    rows = []
    for i, item in enumerate(open_items, 1):
        lines.append(f"{i}. {html.escape(item['text'])}")
        rows.append([
            InlineKeyboardButton(f"✅ {i} Done", callback_data=f"ag_done:{item['id']}"),
            InlineKeyboardButton(f"❌ {i} Missed", callback_data=f"ag_missed:{item['id']}"),
        ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)

def _status_message(items: list) -> str:
    lines = ["Agenda Status:\n"]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {html.escape(STATUS_ICONS[item['status']])} {html.escape(item['text'])}")
    return "\n".join(lines)

async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    open_items = agenda_.get_open()
    if not open_items:
        await update.message.reply_text("No open agenda items. Use /plan to generate one.")
        return
    text, keyboard = _agenda_message(open_items)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

async def cmd_agenda_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    items = agenda_.get_status()
    if not items:
        await update.message.reply_text("No open agenda items. Use /plan to generate one.")
        return
    text = _status_message(items)
    await update.message.reply_text(text, parse_mode="HTML")

async def handle_agenda_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, item_id = query.data.split(":")[0], int(query.data.split(":")[1])
    status = "done" if action == "ag_done" else "missed"
    agenda_.mark_status(item_id, status)

    await _safe_answer(query, _encourage() if status == "done" else "Marked missed.")

    open_items = agenda_.get_open()
    if not open_items:
        await query.edit_message_text("✅ All items resolved.")
        return
    text, keyboard = _agenda_message(open_items)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# --- Proposal callback ---

async def handle_proposal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)

    chat_id = update.effective_chat.id
    if chat_id not in _pending:
        await query.edit_message_text("No pending proposal — use /plan to generate one.")
        return

    state = _pending[chat_id]
    items, selected = state["items"], state["selected"]
    data = query.data

    if data.startswith("pt_t:"):
        idx = int(data.split(":")[1])
        selected.symmetric_difference_update({idx})
        await query.edit_message_text(
            _proposal_text(items, selected),
            parse_mode="HTML",
            reply_markup=_proposal_keyboard(items, selected),
        )

    elif data == "pt_all":
        accepted = list(items)
        _commit_agenda(accepted)
        del _pending[chat_id]
        await query.edit_message_text(f"✅ Accepted all {len(accepted)} items. Agenda set.")

    elif data == "pt_ok":
        accepted = [items[i] for i in sorted(selected)]
        if not accepted:
            del _pending[chat_id]
            await query.edit_message_text("Nothing selected — agenda not set.")
            return
        _commit_agenda(accepted)
        del _pending[chat_id]
        lines = "\n".join(f"• {html.escape(t)}" for t in accepted)
        await query.edit_message_text(f"✅ Agenda set ({len(accepted)} items):\n{lines}", parse_mode="HTML")

    elif data.startswith("pt_e:"):
        idx = int(data.split(":")[1])
        state["editing"] = idx
        await _safe_answer(query, f"Send new text for item {idx + 1}:")
        await query.message.reply_text(f"✏️ Send new text for item {idx + 1}:")

    elif data == "pt_no":
        del _pending[chat_id]
        await query.edit_message_text("Proposal discarded.")


def _commit_agenda(texts: list[str], source: str = "llm"):
    items = agenda_.accept_items(texts, source=source)
    agenda_.write_to_markdown(items)


# --- Message handler ---

_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

def _normalize(text: str) -> str:
    def _replace(w: str) -> str:
        clean = w.strip(".,!?;:")
        return _NUM_WORDS.get(clean, w)
    return " ".join(_replace(w) for w in text.split())


def _parse_time(text: str) -> str | None:
    text = text.strip().lower()
    if text in ("now", "עכשיו"):
        now = datetime.now(ZoneInfo("Asia/Jerusalem"))
        return f"{now.hour:02d}:{now.minute:02d}"
    m = re.search(r'(\d{1,2}):(\d{2})', text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r'(\d{1,2})\s*(am|pm)', text)
    if m:
        h = int(m.group(1))
        if m.group(2) == 'pm' and h != 12:
            h += 12
        elif m.group(2) == 'am' and h == 12:
            h = 0
        return f"{h:02d}:00"
    m = re.match(r'^(\d{1,2})$', text)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


_UNICODE_JUNK = re.compile("[​-‏‪-‮⁠-⁤﻿]+")


async def _process_text(text: str, reply, chat_id: int = 0) -> None:
    text = _UNICODE_JUNK.sub("", text).strip()
    update_chat_id = chat_id
    now = datetime.now().strftime("%H:%M")
    lower = _normalize(text.lower()).strip(".,!?;: ")

    # edit N <text> — update agenda item text
    edit_match = re.match(r"^edit\s+(\d+)\s+(.+)$", lower)
    if edit_match:
        n = int(edit_match.group(1))
        open_items = agenda_.get_open()
        if n < 1 or n > len(open_items):
            await reply(f"No open item #{n}.")
            return
        actual_id = open_items[n - 1]["id"]
        orig_match = re.match(r"^edit\s+\S+\s+(.*?)[\s.,!?;:]*$", text, re.IGNORECASE)
        new_text = orig_match.group(1) if orig_match else edit_match.group(2)
        old_text = agenda_.edit_item(actual_id, new_text)
        logs.write("edit", f"item {n}: '{old_text}' → '{new_text}'")
        await reply(f"✏️ Item {n} updated.")
        return

    # done N / missed N — mark by number
    done_match = re.match(r"^(done|missed)\s+(\d+)$", lower)
    if done_match:
        action, n = done_match.group(1), int(done_match.group(2))
        open_items = agenda_.get_open()
        if n < 1 or n > len(open_items):
            await reply(f"No open item #{n}.")
            return
        actual_id = open_items[n - 1]["id"]
        agenda_.mark_status(actual_id, action)
        icon = "✅" if action == "done" else "❌"
        suffix = f" {_encourage()}" if action == "done" else ""
        await reply(f"{icon} Item {n} marked {action}.{suffix}")
        return

    # done <name> / missed <name> — mark by fuzzy name match
    name_match = re.match(r"^(done|missed)\s+(.+)$", lower)
    if name_match:
        action, query_text = name_match.group(1), name_match.group(2)
        open_items = agenda_.get_open()
        if open_items:
            item_texts = [i["text"].lower() for i in open_items]
            matches = difflib.get_close_matches(query_text, item_texts, n=1, cutoff=0.3)
            if not matches:
                # fallback: substring match
                matches = [t for t in item_texts if query_text in t or t in query_text]
            if matches:
                item = open_items[item_texts.index(matches[0])]
                agenda_.mark_status(item["id"], action)
                icon = "✅" if action == "done" else "❌"
                suffix = f" {_encourage()}" if action == "done" else ""
                await reply(f"{icon} \"{item['text']}\" marked {action}.{suffix}")
                return
        await reply(f"Couldn't match \"{query_text}\" to any open agenda item.")
        return

    # event: / new event / add to calendar / etc — create a Google Calendar event
    _event_pattern = re.match(
        r"^(?:new\s+)?(?:calendar\s+)?event[:\s]+(.+)$"
        r"|^add(?:\s+(?:calendar\s+)?event)[:\s]+(.+)$"
        r"|^add\s+to\s+(?:(?:google\s+)?calendar)[:\s]+(.+)$",
        lower,
    )
    if _event_pattern:
        event_text = next(g for g in _event_pattern.groups() if g is not None)
        # use original text with preserved case, same offset as matched group
        event_text = text[lower.index(event_text):].strip()
        await reply("📅 Parsing event…")
        try:
            parsed = await planner_.parse_event(event_text)
            if not parsed:
                await reply("Couldn't parse the event. Try: new calendar event: dentist tomorrow at 10am")
                return
            tz = ZoneInfo("Asia/Jerusalem")
            start_dt = datetime.fromisoformat(
                f"{parsed['date']}T{parsed['start_time']}:00"
            ).replace(tzinfo=tz)
            event = await asyncio.to_thread(
                gcal_.create_event,
                parsed["summary"],
                start_dt,
                parsed.get("duration_minutes", 60),
                parsed.get("description"),
            )
            link = event.get("htmlLink", "")
            await reply(f"✅ Created: <b>{html.escape(parsed['summary'])}</b> on {parsed['date']} at {parsed['start_time']}\n{link}", )
        except Exception as e:
            await reply(f"Failed to create event: {e}")
        return

    # remind: / remind me — create a recurring reminder
    if lower.startswith("remind:") or lower.startswith("remind me"):
        reminder_text = re.sub(r"^remind(:|(\s+me\b))\s*", "", text, flags=re.IGNORECASE).strip()
        await reply("⏰ Parsing reminder…")
        try:
            parsed = await planner_.parse_reminder(reminder_text)
            if not parsed:
                await reply("Couldn't parse the reminder. Try: remind: eat lunch at 13:00 or remind: drink water every 60 minutes")
                return
            from datetime import date as _date
            extra = {k: v for k, v in parsed.items() if k not in ("text", "type")}
            if parsed["type"] == "once" and "date" not in extra:
                extra["date"] = _date.today().isoformat()
            if parsed["type"] == "weekly" and "day_of_week" in parsed:
                day_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
                extra["day"] = day_map.get(parsed["day_of_week"].lower(), 4)
            if parsed["type"] in ("once", "daily", "weekly") and "time" not in extra:
                # ask for the time rather than defaulting
                _awaiting_time[update_chat_id] = {"text": parsed["text"], "type": parsed["type"], **extra}
                d = extra.get("date", _date.today().isoformat())
                when = "today" if d == _date.today().isoformat() else d
                await reply(f"What time on {when} should I remind you?")
                return
            entry = reminders.add(text=parsed["text"], reminder_type=parsed["type"], **extra)
            if entry["type"] == "once":
                d = entry.get("date", _date.today().isoformat())
                when = "today" if d == _date.today().isoformat() else d
                await reply(f"⏰ Reminder set: \"{entry['text']}\" on {when} at {entry['time']}")
            elif entry["type"] == "daily":
                await reply(f"⏰ Reminder set: \"{entry['text']}\" every day at {entry['time']}")
            elif entry["type"] == "weekly":
                days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
                day_name = days[entry.get("day", 4)]
                await reply(f"⏰ Reminder set: \"{entry['text']}\" every {day_name} at {entry['time']}")
            else:
                ws = entry.get("window_start", "08:00")
                we = entry.get("window_end", "22:00")
                await reply(f"⏰ Reminder set: \"{entry['text']}\" every {entry['interval_minutes']} min ({ws}–{we})")
        except Exception as e:
            await reply(f"Failed to set reminder: {e}")
        return

    # backlog: / someday: — add to backlog
    if re.match(r"^(backlog|someday)[:\s]", lower):
        item_text = re.sub(r"^(backlog|someday)[:\s]\s*", "", text, flags=re.IGNORECASE).strip()
        if item_text:
            backlog_.add(item_text)
            await reply(f"📋 Added to backlog: {item_text}")
            return

    # shabbat / candle lighting — set quiet mode manually
    if re.match(r"^(shabbat mode|candle lighting|shabbos mode)", lower):
        _awaiting_candles[update_chat_id] = True
        await reply("🕯️ What time is candle lighting?")
        return

    # queue for <day> [: | ,] <item> — add to a future agenda (works with voice)
    if re.match(r"^(?:queue|schedule|defer|add to)\b", lower):
        parsed = await _parse_queue_entry(text)
        if parsed:
            target = _parse_queue_date(parsed["day"])
            if target:
                queue_.add(parsed["item"], target)
                await reply(f"📅 Queued for {target.strftime('%A %b %d')}: {parsed['item']}")
                return
        await reply("Couldn't parse that. Try: 'schedule for Sunday: deploy to VPS'")

    # job tracker: <description> — parse and add via Claude (works with voice notes too)
    if re.match(r"^job[-\s]tracker", lower):
        description = re.sub(r"^job[-\s]tracker[:\s]*", "", text, flags=re.IGNORECASE).strip()
        if not description:
            # Start interactive flow
            _awaiting_job[update_chat_id] = {"step": "company", "data": {}}
            await reply("Company name?")
            return
        await reply("📋 Parsing job details…")
        parsed = await _parse_job_from_text(description)
        if not parsed:
            await reply("Couldn't parse that. Try: 'job tracker: Applied to Acme for Backend Engineer via LinkedIn'")
            return
        _awaiting_job[update_chat_id] = {"step": "confirm", "data": parsed}
        await reply(_job_summary(parsed), parse_mode="HTML", reply_markup=_job_confirm_keyboard())
        return

    # add a job — start interactive flow
    if re.match(r"^add\s+a?\s*job\b", lower):
        _awaiting_job[update_chat_id] = {"step": "company", "data": {}}
        await reply("Company name?")
        return

    # add: — user adds their own agenda item
    if lower.startswith("add:"):
        item_text = text[4:].strip()
        _commit_agenda([item_text], source="user")
        await reply(f"Added to agenda: {item_text}")
        return

    # metric: <key> <value> — structured metric entry
    metric_m = re.match(r"^metric[,:.\s]\s*([\w\-]+)\s+(\S+)", text, re.IGNORECASE)
    if metric_m:
        key = metric_m.group(1).lower().replace("-", "_")
        raw_val = metric_m.group(2)
        # If key is numeric and value looks like a word, they're reversed — swap them
        # e.g. "metric: 8000 steps" → key=steps, value=8000
        if re.match(r"^[\d.]+$", key) and re.match(r"^[a-z_]+$", raw_val, re.IGNORECASE):
            key, raw_val = raw_val.lower().replace("-", "_"), key
        num_m = re.match(r"^([\d.]+)", raw_val)
        value = float(num_m.group(1)) if num_m else raw_val
        unit = raw_val[len(num_m.group(1)):] if num_m else ""
        logs.write_metric(key, value, unit)
        await reply(f"📊 Metric logged: {key} = {raw_val}")
        return

    # feedback request — log it and respond with Claude's take
    feedback_m = _FEEDBACK_RE.match(text)
    if feedback_m:
        content = (feedback_m.group(1) or "").strip()
        if not content:
            await reply("What's on your mind? Send your idea or question after 'feedback:'")
            return
        logs.write("feedback", content)
        await reply("💭 Thinking…")
        try:
            response_text = await planner_.feedback(content)
            await reply(response_text)
        except Exception as e:
            await reply(f"Feedback failed: {e}")
        return

    # standard log entry — match prefix keyword regardless of trailing punctuation/case
    tag = "log"
    content = text

    checkin_m = _CHECKIN_RE.match(text)
    if checkin_m:
        tag = "checkin"
        content = (checkin_m.group(1) or "").strip()
    else:
        first_word_m = re.match(r"^(\w+)[,:.\s]\s*(.*)", lower, re.DOTALL)
        first_word = first_word_m.group(1) if first_word_m else ""
        for prefix, t in PREFIXES.items():
            keyword = prefix.rstrip(": ")
            if first_word == keyword or lower.startswith(prefix):
                tag = t.lstrip("#")
                content = re.sub(r"^\w+[,:.\s]\s*", "", text, count=1, flags=re.IGNORECASE).strip()
                break

    # For food entries, try to extract macros from natural language (e.g. voice notes
    # that give per-100g values and a weight). If parsing succeeds, replace raw content
    # with a formatted summary. Falls back to logging as-is if no macro data found.
    if tag == "food":
        try:
            macros = await planner_.parse_food_macros(content)
            if macros:
                parts = [f"{macros['food']} {macros['weight_g']}g"]
                stats = []
                if "kcal" in macros:
                    stats.append(f"{macros['kcal']} kcal")
                if "protein_g" in macros:
                    stats.append(f"{macros['protein_g']}g protein")
                if "fat_g" in macros:
                    stats.append(f"{macros['fat_g']}g fat")
                if "carbs_g" in macros:
                    stats.append(f"{macros['carbs_g']}g carbs")
                if stats:
                    content = parts[0] + " — " + ", ".join(stats)
        except Exception:
            pass

    entry = {
        "ts": datetime.now(ZoneInfo("Asia/Jerusalem")).isoformat(timespec="seconds"),
        "tag": tag,
        "content": content,
    }
    log_file = os.path.join(LOG_DIR, f"{datetime.now().date()}.jsonl")
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if tag == "checkin":
        await reply(f"Logged #{tag} ✓", reply_markup=_mood_energy_keyboard())
    elif tag == "food":
        await reply(f"🍽 Logged: {content}")
    elif tag == "hypothesis":
        await reply("Logged #hypothesis ✓ — thinking about it…")
        try:
            result = await planner_.evaluate_hypothesis(content)

            # Show the narrative
            await reply(result["narrative"])

            # Set up tracking actions
            actions = []

            if result.get("metrics"):
                keys = ", ".join(f"<code>metric: {m['key']} &lt;value&gt;</code>" for m in result["metrics"])
                descs = "\n".join(f"• <b>{m['key']}</b>: {m['description']}" for m in result["metrics"])
                actions.append(f"📊 <b>Track these metrics:</b>\n{descs}\n\nLog with: {keys}")

            if result.get("habits"):
                actions.append("👁 <b>Watch these habits:</b> " + ", ".join(result["habits"]))

            if result.get("follow_up_date"):
                from datetime import date as _date
                fu_date = _date.fromisoformat(result["follow_up_date"])
                reminder_text = f"Hypothesis check-in: {result['follow_up_note']}"
                reminders.add(reminder_text, reminder_type="once",
                              date=result["follow_up_date"], time="10:00")
                actions.append(f"⏰ <b>Follow-up reminder set:</b> {fu_date.strftime('%A %b %d')} — {result['follow_up_note']}")

            if actions:
                await reply("\n\n".join(actions), parse_mode="HTML")

        except Exception as e:
            await reply(f"Hypothesis logged but evaluation failed: {e}")
    else:
        await reply(f"Logged #{tag} ✓")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # intercept edit reply for pending proposal
    if chat_id in _pending and "editing" in _pending[chat_id]:
        idx = _pending[chat_id].pop("editing")
        _pending[chat_id]["items"][idx] = text
        state = _pending[chat_id]
        await update.message.reply_text(
            _proposal_text(state["items"], state["selected"]),
            parse_mode="HTML",
            reply_markup=_proposal_keyboard(state["items"], state["selected"]),
        )
        return

    # intercept voice transcript edit
    if chat_id in _awaiting_voice_edit and _awaiting_voice_edit[chat_id] == "__edit__":
        _awaiting_voice_edit[chat_id] = text.strip()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ OK", callback_data="voice_ok"),
            InlineKeyboardButton("✏️ Edit", callback_data="voice_edit"),
        ]])
        await update.message.reply_text(f'🎙 "{text.strip()}"', reply_markup=keyboard)
        return

    # intercept context file edit
    if chat_id in _awaiting_context:
        fname = _awaiting_context.pop(chat_id)
        if text.strip().lower() == "/cancel":
            await update.message.reply_text("Edit cancelled.")
            return
        context_.write(fname, text)
        title = fname.replace(".md", "").title()
        await update.message.reply_text(f"✅ {title} updated.")
        return

    # intercept candle lighting time
    if _awaiting_candles.pop(chat_id, False):
        t = _parse_time(text)
        if t:
            _save_candle_lighting(t)
            h, m = int(t[:2]), int(t[3:])
            quiet_m = m - 20 if m >= 20 else m + 40
            quiet_h = h if m >= 20 else h - 1
            quiet = f"{quiet_h:02d}:{quiet_m:02d}"
            now_t = datetime.now(ZoneInfo("Asia/Jerusalem"))
            already = now_t.hour * 60 + now_t.minute >= quiet_h * 60 + quiet_m
            msg = f"🕯️ Candle lighting set for {t}. Shabbat Shalom — {'already in quiet mode.' if already else f'going quiet at {quiet}.'}"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("Couldn't parse that time. Send it again (e.g. 19:45).")
            _awaiting_candles[chat_id] = True
        return

    # intercept backlog→queue day reply
    if chat_id in _awaiting_job and _awaiting_job[chat_id].get("step") == "bl_day":
        state = _awaiting_job.pop(chat_id)
        target = _parse_queue_date(text.strip())
        item_id = state["data"]["item_id"]
        item_text = state["data"]["text"]
        if not target:
            await update.message.reply_text("Couldn't parse that day. Try: Sunday, Monday, tomorrow…")
            return
        queue_.add(item_text, target)
        backlog_.remove(item_id)
        await update.message.reply_text(f"📅 Queued for {target.strftime('%A %b %d')}: {html.escape(item_text)}", parse_mode="HTML")
        return

    # intercept job entry flow
    if chat_id in _awaiting_job:
        if text.strip().lower() == "/cancel":
            del _awaiting_job[chat_id]
            await update.message.reply_text("Job entry cancelled.")
            return
        next_prompt = _job_flow_next(chat_id, text.strip())
        if next_prompt:
            await update.message.reply_text(next_prompt)
        else:
            # ready to confirm
            state = _awaiting_job[chat_id]
            state["step"] = "confirm"
            await update.message.reply_text(
                _job_summary(state["data"]), parse_mode="HTML",
                reply_markup=_job_confirm_keyboard(),
            )
        return

    # intercept time reply for pending reminder
    if chat_id in _awaiting_time:
        partial = _awaiting_time.pop(chat_id)
        t = _parse_time(text)
        if not t:
            await update.message.reply_text("Couldn't parse that as a time. Reminder cancelled.")
            return
        from datetime import date as _date
        entry = reminders.add(text=partial["text"], reminder_type=partial["type"],
                                   **{k: v for k, v in partial.items() if k not in ("text", "type")},
                                   time=t)
        d = entry.get("date", _date.today().isoformat())
        when = "today" if d == _date.today().isoformat() else d
        await update.message.reply_text(f"⏰ Reminder set: \"{entry['text']}\" on {when} at {t}")
        return

    await _process_text(text, update.message.reply_text, chat_id=chat_id)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return

    tg_file = await update.message.voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as audio:
            transcript = openai.OpenAI().audio.transcriptions.create(
                model="whisper-1", file=audio
            )
        text = transcript.text.strip()
    finally:
        os.unlink(tmp_path)

    chat_id = update.effective_chat.id
    _awaiting_voice_edit[chat_id] = text
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ OK", callback_data="voice_ok"),
        InlineKeyboardButton("✏️ Edit", callback_data="voice_edit"),
    ]])
    await update.message.reply_text(f'🎙 "{text}"', reply_markup=keyboard)


# --- Scheduled morning plan ---

async def morning_plan():
    if _shabbat_quiet_now():
        return
    await _send_proposal(ALLOWED_USER)
    # Friday: ask for candle lighting time
    if datetime.now(ZoneInfo("Asia/Jerusalem")).weekday() == 4:
        if not _load_candle_lighting():
            await _bot.send_message(
                chat_id=ALLOWED_USER,
                text="🕯️ What time is candle lighting today?",
            )
            _awaiting_candles[ALLOWED_USER] = True


async def _send_proposal(chat_id: int):
    calendar_events = ""
    try:
        events = await asyncio.to_thread(gcal_.get_today_events)
        calendar_events = gcal_.format_events(events)
    except Exception:
        pass  # calendar is optional — plan without it if unavailable

    # inject any items queued for today
    queued = queue_.pop_for_today()
    if queued:
        agenda_.accept_items(queued, source="queued")

    try:
        items = await agenda_.generate(planner_, calendar_events)
    except Exception as e:
        await _bot.send_message(chat_id=chat_id, text=f"Agenda generation failed: {e}")
        return

    if not items:
        await _bot.send_message(chat_id=chat_id, text="No agenda items returned — try again.")
        return

    selected = set(range(len(items)))
    _pending[chat_id] = {"items": items, "selected": selected}

    await _bot.send_message(
        chat_id=chat_id,
        text=_proposal_text(items, selected),
        parse_mode="HTML",
        reply_markup=_proposal_keyboard(items, selected),
    )


QUIET_START = time(0, 0)   # 00:00
QUIET_END   = time(8, 0)   # 08:00
EVENT_QUIET_END = time(22, 0)  # 22:00


def _candles_path() -> str:
    from datetime import date as _date
    return os.path.join(LOG_DIR, f"{_date.today()}-candles.txt")


def _save_candle_lighting(t: str):
    with open(_candles_path(), "w") as f:
        f.write(t)


def _load_candle_lighting() -> time | None:
    path = _candles_path()
    if not os.path.exists(path):
        return None
    try:
        raw = open(path).read().strip()
        h, m = map(int, raw.split(":"))
        return time(h, m)
    except Exception:
        return None


SHABBAT_END_HOUR = 21  # assumed nightfall; replace with Zmanim API eventually


def _shabbat_quiet_now() -> bool:
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    weekday = now.weekday()
    if weekday == 5:  # Saturday — quiet until assumed nightfall
        return now.hour < SHABBAT_END_HOUR
    if weekday == 4:  # Friday — quiet from 20 min before candle lighting
        candles = _load_candle_lighting()
        if candles:
            quiet_dt = datetime.combine(now.date(), candles, tzinfo=ZoneInfo("Asia/Jerusalem")) - timedelta(minutes=20)
            if now >= quiet_dt:
                return True
    return False


def _in_active_window() -> bool:
    now_t = datetime.now(ZoneInfo("Asia/Jerusalem")).time().replace(second=0, microsecond=0)
    return QUIET_END <= now_t <= EVENT_QUIET_END


async def remind_upcoming():
    if _shabbat_quiet_now() or not _in_active_window():
        return
    try:
        events = await asyncio.to_thread(gcal_.get_upcoming_events, within_minutes=15)
    except Exception:
        return
    reminded = _load_reminded()
    for event in events:
        eid = event.get("id")
        if eid in reminded:
            continue
        _save_reminded(eid)
        start = event["start"].get("dateTime", "")
        summary = event.get("summary", "(no title)")
        if start:
            t = datetime.fromisoformat(start).astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
            msg = f"⏰ Reminder: <b>{html.escape(summary)}</b> at {t}"
        else:
            msg = f"⏰ Reminder: <b>{html.escape(summary)}</b> starting soon"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Dismiss", callback_data="remind_dismiss")]])
        await _bot.send_message(chat_id=ALLOWED_USER, text=msg, parse_mode="HTML", reply_markup=keyboard)


async def handle_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    is_checkin = query.data == "remind_dismiss_c"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    if is_checkin:
        logs.write("checkin", "reminder dismissed")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="👋",
            reply_markup=_mood_energy_keyboard(),
        )


HELP_TEXT = """<b>Planning</b>
/plan — generate today's agenda (also runs daily at 06:00)
/agenda — open items with ✅ Done / ❌ Missed buttons
/status — all items with their current status (done / missed / open)

<b>Calendar</b>
/events — upcoming events for today
<code>event: &lt;description&gt;</code> — create a Google Calendar event
  e.g. <i>new calendar event: PTA meeting April 13th at 4:20pm</i>
  e.g. <i>add to calendar: dentist tomorrow at 10am</i>

<b>Reminders</b>
/reminders — list all reminders (tap 🗑 to delete)
<code>remind me &lt;...&gt;</code> — set a reminder
  e.g. <i>remind me at 3pm to start a walk</i>
  e.g. <i>remind me every 60 minutes to drink water</i>
  e.g. <i>remind me of my meeting on June 15th</i>

<b>Agenda</b>
/queue — view queued future agenda items
<code>schedule for Sunday: &lt;item&gt;</code> — add item to a future day's agenda
<code>done &lt;N or name&gt;</code> — mark item done
<code>missed &lt;N or name&gt;</code> — mark item missed
<code>add: &lt;text&gt;</code> — add your own agenda item
<code>edit &lt;N&gt; &lt;new text&gt;</code> — edit an agenda item

<b>Habits</b>
/habits — today's habit checklist (from habits.md)
/habitlog — generate today's habit log file for Obsidian (done + streaks pre-filled, add notes manually)
<code>habit: &lt;name&gt;</code> — log a completed habit (e.g. <i>habit: walk</i>, <i>habit: daf yomi</i>)
<code>skip: &lt;reason&gt;</code> — log an external constraint that excused habits today (e.g. <i>skip: chavrusa cancelled</i>)

<b>Job Search</b>
/jobs — job application status summary
<code>job tracker: &lt;description&gt;</code> — add application from text or voice note
  e.g. <i>job tracker: Applied to Acme for Backend Engineer via LinkedIn</i>
<code>add a job</code> — interactive step-by-step entry

<b>Review</b>
/daily — end-of-day digest with quote, wins, and suggestions (also runs nightly at 22:30)
/digest — weekly AI review of the last 7 days (also runs every Sunday at 20:00)
/metrics — tracked metrics with trend (last 14 days)
/logs — view today's log entries

<b>Context</b>
/context — view and edit your goals, priorities, constraints, projects, principles

<b>Logging</b>
<code>food: &lt;what you ate&gt;</code> — log a meal (/food shows today's food log)
<code>metric: &lt;key&gt; &lt;value&gt;</code> — log a metric (e.g. <i>metric: steps 8000</i>)
<code>did: &lt;text&gt;</code> — log a spontaneous win (tagged <code>#win</code>)
<code>feedback: &lt;idea or question&gt;</code> — get Claude's take (also: "feedback request", "question")
<code>note: / insight: / task: / hypothesis: / checkin</code>
Anything else is logged as <code>#log</code>

Voice notes are transcribed automatically."""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


_DIGEST_TEMPLATE = context_.dir / "templates" / "digest-template.md"
_DIGEST_DIR = context_.dir / "digests"


def _digest_to_html(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^#{1,3}\s*", "", line)
        if line.strip() == "---":
            lines.append("")
            continue
        # escape plain text segments, preserve bold/italic as HTML tags
        result = ""
        last = 0
        for m in re.finditer(r"\*\*(.+?)\*\*|\*(.+?)\*", line):
            result += html.escape(line[last:m.start()])
            if m.group(1) is not None:
                result += f"<b>{html.escape(m.group(1))}</b>"
            else:
                result += f"<i>{html.escape(m.group(2))}</i>"
            last = m.end()
        result += html.escape(line[last:])
        lines.append(result)
    return "\n".join(lines).strip()


def _save_digest(text: str, label: str = "digest") -> None:
    _DIGEST_DIR.mkdir(exist_ok=True)
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    template = _DIGEST_TEMPLATE.read_text() if _DIGEST_TEMPLATE.exists() else "---\ntitle:\ngenerated: \"{{DATETIME}}\"\ntype: digest\n---\n"
    filled = template.replace("{{DATETIME}}", now.isoformat(timespec="seconds"))
    date_str = now.date().isoformat()
    filled = filled.replace("title:", f"title: {date_str} {label}")
    path = _DIGEST_DIR / f"{date_str}-{label}.md"
    path.write_text(filled + "\n" + text + "\n")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text("🔍 Generating digest…")
    try:
        baseline_.compute_and_save_weekly(logs)
        text = await planner_.digest()
        _save_digest(text, label="digest")
        await update.message.reply_text(_digest_to_html(text), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Digest failed: {e}")


def _digest_target_date() -> "date":
    from datetime import date as _date, timedelta as _td
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    # Before 6am counts as end of the previous day, not start of the new one
    if now.hour < 6:
        return _date.today() - _td(days=1)
    return _date.today()


async def cmd_daily_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    from datetime import date as _date, timedelta as _td
    arg = " ".join(context.args).strip().lower() if context.args else ""
    if arg in ("yesterday", "y"):
        target = _date.today() - _td(days=1)
    else:
        target = _digest_target_date()

    label = "today" if target == _date.today() else str(target)
    await update.message.reply_text(f"🔍 Generating daily digest for {label}…")
    try:
        text = await planner_.daily_digest(target_date=target)
        _save_digest(text, label="daily")
        await update.message.reply_text(_digest_to_html(text), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Daily digest failed: {e}")


async def scheduled_daily_digest():
    if _shabbat_quiet_now():
        return
    try:
        text = await planner_.daily_digest(target_date=_digest_target_date())
        _save_digest(text, label="daily")
        await _bot.send_message(
            chat_id=ALLOWED_USER,
            text=f"🌙 <b>Daily digest:</b>\n\n{_digest_to_html(text)}",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def weekly_digest():
    if _shabbat_quiet_now():
        return
    try:
        baseline_.compute_and_save_weekly(logs)
        text = await planner_.digest()
        _save_digest(text, label="weekly-digest")
        await _bot.send_message(chat_id=ALLOWED_USER, text=f"📋 <b>Weekly digest:</b>\n\n{_digest_to_html(text)}", parse_mode="HTML")
    except Exception:
        pass


async def cmd_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    data = logs.load_metrics(days=14)
    if not data:
        await update.message.reply_text("No metrics logged yet. Use: metric: steps 8000")
        return
    lines = ["📊 <b>Metrics (last 14 days):</b>\n"]
    for key, entries in sorted(data.items()):
        numeric = [v for _, v in entries if isinstance(v, (int, float))]
        last_date, last_val = entries[-1]
        unit = ""
        # try to recover unit from last entry
        for e in reversed(entries):
            if isinstance(e[1], (int, float)):
                break
        trend = ""
        if len(numeric) >= 3:
            if numeric[-1] > numeric[-3]:
                trend = " ↑"
            elif numeric[-1] < numeric[-3]:
                trend = " ↓"
            else:
                trend = " →"
        avg = f" | avg {sum(numeric)/len(numeric):.1f}" if len(numeric) > 1 else ""
        recent = ", ".join(str(v) for _, v in entries[-5:])
        lines.append(f"<b>{key}</b>: {recent}{avg}{trend}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    from datetime import date as _date
    log_file = os.path.join(LOG_DIR, f"{_date.today()}.jsonl")
    if not os.path.exists(log_file):
        await update.message.reply_text("No log entries today.")
        return
    lines = []
    for line in open(log_file):
        try:
            e = json.loads(line)
            t = e["ts"][11:16]  # HH:MM from ISO timestamp
            lines.append(f"<code>{t}</code> <b>#{e['tag']}</b> {html.escape(e['content'])}")
        except Exception:
            pass
    if not lines:
        await update.message.reply_text("No log entries today.")
        return
    text = f"📋 <b>Today's log ({len(lines)} entries):</b>\n\n" + "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(truncated)"
    await update.message.reply_text(text, parse_mode="HTML")


MOOD_OPTIONS    = [("😄","great"), ("😊","good"), ("😐","okay"), ("😕","low"), ("😞","bad")]
ENERGY_OPTIONS  = [("⚡","high"),  ("🔋","okay"), ("🪫","drained")]


def _mood_energy_keyboard(locked_mood: str = "", locked_energy: str = "") -> InlineKeyboardMarkup:
    mood_row = [
        InlineKeyboardButton(
            f"✅ {e}" if locked_mood == v else f"{e}",
            callback_data="noop" if locked_mood == v else f"me_mood:{e}:{v}",
        )
        for e, v in MOOD_OPTIONS
    ]
    energy_row = [
        InlineKeyboardButton(
            f"✅ {e}" if locked_energy == v else f"{e}",
            callback_data="noop" if locked_energy == v else f"me_energy:{e}:{v}",
        )
        for e, v in ENERGY_OPTIONS
    ]
    return InlineKeyboardMarkup([mood_row, energy_row])


async def handle_mood_energy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    parts = query.data.split(":")  # me_mood:😊:good  or  me_energy:⚡:high
    kind, emoji, value = parts[0][3:], parts[1], parts[2]  # strip "me_"
    _mood_scores   = {"great": 5, "good": 4, "okay": 3, "low": 2, "bad": 1}
    _energy_scores = {"high": 3, "okay": 2, "drained": 1}
    numeric = _mood_scores.get(value) if kind == "mood" else _energy_scores.get(value)
    logs.write_metric(kind, numeric if numeric is not None else value)
    # rebuild keyboard with this selection locked, other row still active
    locked_mood    = value if kind == "mood"   else ""
    locked_energy  = value if kind == "energy" else ""
    # carry over previously locked value from existing keyboard if present
    for row in (query.message.reply_markup.inline_keyboard or []):
        for btn in row:
            if btn.callback_data == "noop" and btn.text.startswith("✅"):
                for e, v in MOOD_OPTIONS:
                    if e in btn.text and not locked_mood:
                        locked_mood = v
                for e, v in ENERGY_OPTIONS:
                    if e in btn.text and not locked_energy:
                        locked_energy = v
    try:
        await query.edit_message_reply_markup(
            reply_markup=_mood_energy_keyboard(locked_mood, locked_energy)
        )
    except Exception:
        pass


def _habit_matches(logged: str, canonical: str) -> bool:
    """True if any significant word (≥3 chars) from logged appears in canonical or vice versa."""
    logged_words = {w for w in re.split(r"\W+", logged.lower()) if len(w) >= 3}
    canonical_words = {w for w in re.split(r"\W+", canonical.lower()) if len(w) >= 3}
    return bool(logged_words & canonical_words)


def _habits_message() -> tuple[str, InlineKeyboardMarkup]:
    from datetime import date as _date
    today_weekday = _date.today().weekday()
    sections = context_.parse_habits()
    logged_today = [e["content"].strip() for e in logs.read_today() if e.get("tag") == "habit"]

    lines = ["📋 <b>Habits</b>\n"]
    rows = []
    for section, habits in sections.items():
        visible = [h for h in habits if h["days"] is None or today_weekday in h["days"]]
        if not visible:
            continue
        lines.append(f"<b>{html.escape(section)}</b>")
        for h in visible:
            name = context_.habit_display_name(h["text"])
            done = any(_habit_matches(logged, h["raw"]) for logged in logged_today)
            lines.append(f"{'✅' if done else '⬜'} {html.escape(name)}")
            if not done:
                key = name[:52]  # callback_data max 64 bytes; "hb_done:" = 8
                rows.append([InlineKeyboardButton(f"✅ {name}", callback_data=f"hb_done:{key}")])
        lines.append("")

    return "\n".join(lines).strip(), InlineKeyboardMarkup(rows)


async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    sections = context_.parse_habits()
    if not sections:
        await update.message.reply_text("No habits defined. Edit habits.md in your vault.")
        return
    text, keyboard = _habits_message()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    chat_id = query.message.chat_id
    action = query.data

    if action == "job_cancel":
        _awaiting_job.pop(chat_id, None)
        await query.edit_message_text("Job entry cancelled.")
        return

    if chat_id not in _awaiting_job:
        await query.edit_message_text("Session expired — start again.")
        return

    if action == "job_confirm":
        from jobs import add_application
        data = _awaiting_job.pop(chat_id)["data"]
        app = add_application(
            company=data.get("company", ""),
            title=data.get("title", ""),
            url=data.get("url", ""),
            source=data.get("source", ""),
            notes=data.get("notes", ""),
            status=data.get("status", "applied"),
            applied_date=data.get("applied_date", ""),
        )
        await query.edit_message_text(
            f"✅ Saved: <b>{html.escape(app.company_name)}</b> — {html.escape(app.job_title or '(no title)')}",
            parse_mode="HTML",
        )
        # push to job_tracker git repo in background
        import subprocess, threading
        def _git_push():
            from jobs import APPLICATIONS_CSV
            repo = str(APPLICATIONS_CSV.parent.parent)
            subprocess.run(["git", "-C", repo, "add", "data/applications.csv"], capture_output=True)
            subprocess.run(["git", "-C", repo, "commit", "-m", f"add: {app.company_name}"], capture_output=True)
            subprocess.run(["git", "-C", repo, "push"], capture_output=True)
        threading.Thread(target=_git_push, daemon=True).start()

    elif action == "job_edit_title":
        _awaiting_job[chat_id]["step"] = "edit_title"
        await query.edit_message_text("Send the job title:")


async def handle_habit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    habit_name = query.data.split(":", 1)[1]
    logs.write("habit", habit_name)
    text, keyboard = _habits_message()
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _parse_queue_entry(text: str) -> dict | None:
    from datetime import date as _date
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=[{
            "name": "queue_agenda_item",
            "description": "Extract the target day and agenda item text from a queue/defer request",
            "input_schema": {
                "type": "object",
                "properties": {
                    "day":  {"type": "string", "description": "Day name or date, e.g. 'Sunday', 'Monday', 'tomorrow'"},
                    "item": {"type": "string", "description": "The agenda item text to queue"},
                },
                "required": ["day", "item"],
            },
        }],
        tool_choice={"type": "tool", "name": "queue_agenda_item"},
        messages=[{"role": "user", "content": f"Today is {_date.today()}. Parse this: {text}"}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None


def _parse_queue_date(day_str: str):
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    day_str = day_str.strip().lower()
    weekdays = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
    # "sunday", "next monday", etc.
    for name, num in weekdays.items():
        if name in day_str:
            days_ahead = (num - today.weekday()) % 7 or 7
            return today + _td(days=days_ahead)
    if day_str in ("tomorrow", "tmrw"):
        return today + _td(days=1)
    # try ISO date
    try:
        return _date.fromisoformat(day_str)
    except ValueError:
        pass
    return None


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    pending = queue_.pending()
    if not pending:
        await update.message.reply_text("No items queued.")
        return
    lines = ["📅 <b>Queued agenda items:</b>\n"]
    for item in pending:
        lines.append(f"• {item['date']} — {html.escape(item['text'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    from jobs import status_summary
    await update.message.reply_text(status_summary(), parse_mode="HTML")


async def _parse_job_from_text(text: str) -> dict | None:
    """Use Claude to extract job fields from a natural language description."""
    from datetime import date as _date
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[{
            "name": "add_job_application",
            "description": "Extract job application fields from a natural language description",
            "input_schema": {
                "type": "object",
                "properties": {
                    "company":      {"type": "string"},
                    "title":        {"type": "string", "description": "Job title — leave blank if unknown"},
                    "status":       {"type": "string", "enum": ["applied", "phone_screen", "interview", "rejected", "withdrew", "offer"], "description": "Default: applied"},
                    "applied_date": {"type": "string", "description": f"YYYY-MM-DD. Resolve relative dates against today ({_date.today()}). Leave blank if not mentioned."},
                    "url":          {"type": "string", "description": "Job posting URL if mentioned"},
                    "source":       {"type": "string", "description": "Where found, e.g. LinkedIn, direct"},
                    "notes":        {"type": "string", "description": "Any extra context, e.g. number of interviews"},
                },
                "required": ["company"],
            },
        }],
        tool_choice={"type": "tool", "name": "add_job_application"},
        messages=[{"role": "user", "content": f"Today is {_date.today()}. Extract job application details: {text}"}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None


def _job_summary(data: dict) -> str:
    lines = ["📋 <b>Job entry — confirm or fill in blanks:</b>\n"]
    lines.append(f"🏢 Company: <b>{html.escape(data.get('company', '?'))}</b>")
    lines.append(f"💼 Title: <b>{html.escape(data.get('title', '') or '—')}</b>")
    lines.append(f"📊 Status: {data.get('status', 'applied')}")
    if data.get('applied_date'):
        lines.append(f"📅 Date: {data['applied_date']}")
    if data.get('source'):
        lines.append(f"🔗 Source: {data['source']}")
    if data.get('notes'):
        lines.append(f"📝 Notes: {data['notes']}")
    return "\n".join(lines)


def _job_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save", callback_data="job_confirm"),
        InlineKeyboardButton("✏️ Edit title", callback_data="job_edit_title"),
        InlineKeyboardButton("❌ Cancel", callback_data="job_cancel"),
    ]])


def _job_flow_next(chat_id: int, value: str) -> str | None:
    """Advance the job entry flow. Returns next prompt or None when ready to confirm."""
    state = _awaiting_job[chat_id]
    step = state["step"]

    if step == "company":
        state["data"]["company"] = value
        state["step"] = "title"
        return "Job title?"
    elif step == "title":
        state["data"]["title"] = value
        state["step"] = "confirm"
        return None
    elif step == "url":
        state["data"]["url"] = "" if value.lower() in ("skip", "-", "") else value
        state["step"] = "source"
        return "Source? (LinkedIn, direct, etc. — or 'skip')"
    elif step == "source":
        state["data"]["source"] = "" if value.lower() in ("skip", "-", "") else value
        state["step"] = "confirm"
        return None
    elif step == "edit_title":
        state["data"]["title"] = value
        state["step"] = "confirm"
        return None
    return None


def _backlog_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        short = item["text"] if len(item["text"]) <= 30 else item["text"][:27] + "…"
        rows.append([
            InlineKeyboardButton(f"📋 {short}", callback_data="noop"),
            InlineKeyboardButton("📅", callback_data=f"bl_queue:{item['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"bl_del:{item['id']}"),
        ])
    return InlineKeyboardMarkup(rows)


async def cmd_backlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    items = backlog_.load()
    if not items:
        await update.message.reply_text("Backlog is empty. Use <code>backlog: idea or task</code> to add.", parse_mode="HTML")
        return
    text = f"📋 <b>Backlog ({len(items)} items):</b>"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_backlog_keyboard(items))


async def handle_backlog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    action, item_id = query.data.split(":", 1)

    if action == "bl_del":
        backlog_.remove(item_id)
        items = backlog_.load()
        if not items:
            await query.edit_message_text("Backlog is empty.")
            return
        await query.edit_message_reply_markup(reply_markup=_backlog_keyboard(items))

    elif action == "bl_queue":
        item = backlog_.get(item_id)
        if not item:
            await query.edit_message_text("Item not found.")
            return
        _awaiting_job[query.message.chat_id] = {
            "step": "bl_day",
            "data": {"item_id": item_id, "text": item["text"]},
        }
        await query.edit_message_text(f"📅 Queue <b>{html.escape(item['text'])}</b> for which day?", parse_mode="HTML")

    elif action == "bl_confirm":
        # item_id encodes "id:day_str"
        parts = item_id.split(":", 1)
        bid, day_str = parts[0], parts[1]
        item = backlog_.get(bid)
        if item:
            target = _parse_queue_date(day_str)
            if target:
                queue_.add(item["text"], target)
                backlog_.remove(bid)
                items = backlog_.load()
                msg = f"📅 Queued for {target.strftime('%A %b %d')}: {html.escape(item['text'])}"
                if items:
                    await query.edit_message_text(msg, parse_mode="HTML", reply_markup=_backlog_keyboard(items))
                else:
                    await query.edit_message_text(msg, parse_mode="HTML")


async def cmd_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    entries = [e for e in logs.read_today() if e.get("tag") == "food"]
    if not entries:
        await update.message.reply_text("Nothing logged yet today. Use <code>food: what you ate</code>.", parse_mode="HTML")
        return
    lines = ["🍽 <b>Today's food log:</b>\n"]
    for e in entries:
        t = e["ts"][11:16]
        lines.append(f"<code>{t}</code> {html.escape(e['content'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_habit_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    from datetime import date as _date, timedelta as _td
    from habit_tracker import generate_habit_log
    arg = " ".join(context.args).strip().lower() if context.args else ""
    target = _date.today() - _td(days=1) if arg in ("yesterday", "y") else _date.today()
    template = context_.dir / "templates" / "habit-template.md"
    output_dir = context_.dir / "habits"
    try:
        path = generate_habit_log(logs, template, output_dir, target)
        await update.message.reply_text(f"✅ Habit log saved: {path.name}\nOpen in Obsidian to add notes.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    rows = [
        [InlineKeyboardButton(f.replace(".md", "").title(), callback_data=f"ctx_view:{f}")]
        for f in context_.files()
    ]
    await update.message.reply_text("📁 <b>Context files:</b>", parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(rows))


async def handle_context_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    data = query.data

    if data.startswith("ctx_view:"):
        fname = data.split(":", 1)[1]
        content = context_.read(fname) or "(empty)"
        title = fname.replace(".md", "").title()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Edit", callback_data=f"ctx_edit:{fname}"),
            InlineKeyboardButton("« Back", callback_data="ctx_back"),
        ]])
        text = f"📄 <b>{title}</b>\n\n{html.escape(content)}"
        if len(text) > 4000:
            text = text[:4000] + "\n…(truncated)"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    elif data.startswith("ctx_edit:"):
        fname = data.split(":", 1)[1]
        _awaiting_context[query.message.chat_id] = fname
        title = fname.replace(".md", "").title()
        await query.edit_message_text(
            f"✏️ Send new content for <b>{title}</b>.\n\n<i>This replaces the entire file. Send /cancel to abort.</i>",
            parse_mode="HTML",
        )

    elif data == "ctx_back":
        rows = [
            [InlineKeyboardButton(f.replace(".md", "").title(), callback_data=f"ctx_view:{f}")]
            for f in context_.files()
        ]
        await query.edit_message_text("📁 <b>Context files:</b>", parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup(rows))


async def handle_voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    chat_id = query.message.chat_id

    if query.data == "voice_ok":
        text = _awaiting_voice_edit.pop(chat_id, None)
        if not text or text == "__edit__":
            await query.edit_message_text("⚠️ No pending transcript.")
            return
        await query.edit_message_text(f'🎙 "{text}"')
        await _process_text(text, lambda msg, **kw: context.bot.send_message(chat_id=chat_id, text=msg, **kw), chat_id=chat_id)

    elif query.data == "voice_edit":
        current = _awaiting_voice_edit.get(chat_id, "")
        _awaiting_voice_edit[chat_id] = "__edit__"
        await query.edit_message_text(
            f"✏️ Copy, edit, and send back:\n\n<code>{html.escape(current)}</code>",
            parse_mode="HTML",
        )


async def check_reminders():
    if _shabbat_quiet_now():
        return
    due = reminders.due_now()
    for r in due:
        if r.get("auto_log"):
            logs.write("reminder", r["text"])
        is_checkin = any(w in r["text"].lower() for w in ("check in", "checkin", "check-in"))
        cb = "remind_dismiss_c" if is_checkin else "remind_dismiss"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Dismiss", callback_data=cb)]])
        await _bot.send_message(
            chat_id=ALLOWED_USER,
            text=f"⏰ <b>{html.escape(r['text'])}</b>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# --- APScheduler lifecycle ---

async def _post_init(application):
    global _bot
    _bot = application.bot
    tz = ZoneInfo("Asia/Jerusalem")
    _scheduler.add_job(morning_plan, "cron", hour=PLAN_HOUR, minute=PLAN_MINUTE, id="morning_plan", replace_existing=True)
    _scheduler.add_job(remind_upcoming, "interval", seconds=600, id="remind_upcoming", replace_existing=True)
    _scheduler.add_job(check_reminders, "interval", seconds=60, id="check_reminders", replace_existing=True)
    _scheduler.add_job(scheduled_daily_digest, "cron", hour=22, minute=30, id="daily_digest", replace_existing=True)
    _scheduler.add_job(weekly_digest, "cron", day_of_week="sun", hour=20, minute=0, id="weekly_digest", replace_existing=True)
    _scheduler.start()


async def _post_shutdown(application):
    _scheduler.shutdown(wait=False)


# --- Entry point ---

def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("habits", cmd_habits))
    app.add_handler(CommandHandler("habitlog", cmd_habit_log))
    app.add_handler(CommandHandler("food", cmd_food))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("metrics", cmd_metrics))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("daily", cmd_daily_digest))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("backlog", cmd_backlog))
    app.add_handler(CallbackQueryHandler(handle_backlog_callback, pattern="^bl_"))
    app.add_handler(CommandHandler("status", cmd_agenda_status))
    app.add_handler(CallbackQueryHandler(handle_proposal_callback, pattern="^pt_"))
    app.add_handler(CallbackQueryHandler(handle_agenda_callback, pattern="^ag_"))
    app.add_handler(CallbackQueryHandler(handle_dismiss, pattern="^remind_dismiss"))
    app.add_handler(CallbackQueryHandler(handle_reminder_delete, pattern="^rm_del:"))
    app.add_handler(CallbackQueryHandler(handle_context_callback, pattern="^ctx_"))
    app.add_handler(CallbackQueryHandler(handle_habit_callback, pattern="^hb_done:"))
    app.add_handler(CallbackQueryHandler(handle_job_callback, pattern="^job_"))
    app.add_handler(CallbackQueryHandler(handle_mood_energy_callback, pattern="^me_"))
    app.add_handler(CallbackQueryHandler(handle_voice_callback, pattern="^voice_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_error_handler)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling()


if __name__ == "__main__":
    main()

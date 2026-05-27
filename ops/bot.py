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

import openai
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
from reminders import Reminders

TOKEN = os.environ["OPS_BOT_TOKEN"]
ALLOWED_USER = int(os.environ["OPS_CHAT_ID"])
MODEL = os.environ.get("OPS_MODEL", "claude-haiku-4-5-20251001")
PLAN_HOUR = int(os.environ.get("OPS_PLAN_HOUR", "8"))
PLAN_MINUTE = int(os.environ.get("OPS_PLAN_MINUTE", "0"))

cwd = os.getcwd()
LOG_DIR = os.path.expanduser(f"{cwd}/ops/log")

# --- Service instances ---
logs      = Logs(LOG_DIR)
agenda_   = Agenda(LOG_DIR)
reminders = Reminders()
gcal_     = GCal()
context_  = Context()
planner_  = Planner(MODEL, logs, context_)

PREFIXES = {
    "insight:":    "#insight",
    "hypothesis:": "#hypothesis",
    "checkin":     "#checkin",
    "task:":       "#task",
    "note:":       "#note",
    "did:":        "#win",
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
_awaiting_time: dict = {}    # chat_id -> partial reminder dict waiting for a time reply
_awaiting_context: dict = {} # chat_id -> filename waiting for new content

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
    await _send_proposal(update.effective_chat.id, context)


def _reminder_label(r: dict) -> str:
    text = r["text"]
    short = text if len(text) <= 20 else text[:17] + "…"
    if r["type"] == "once":
        return f"⏰ {short} — {r.get('date', 'today')} {r.get('time', '?')}"
    elif r["type"] == "daily":
        return f"⏰ {short} — daily {r['time']}"
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
            if parsed["type"] in ("once", "daily") and "time" not in extra:
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
            else:
                ws = entry.get("window_start", "08:00")
                we = entry.get("window_end", "22:00")
                await reply(f"⏰ Reminder set: \"{entry['text']}\" every {entry['interval_minutes']} min ({ws}–{we})")
        except Exception as e:
            await reply(f"Failed to set reminder: {e}")
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

    entry = {
        "ts": datetime.now(ZoneInfo("Asia/Jerusalem")).isoformat(timespec="seconds"),
        "tag": tag,
        "content": content,
    }
    log_file = os.path.join(LOG_DIR, f"{datetime.now().date()}.jsonl")
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

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

    await update.message.reply_text(f'🎙 "{text}"')
    await _process_text(text, update.message.reply_text, chat_id=update.effective_chat.id)


# --- Scheduled morning plan ---

async def morning_plan(context: ContextTypes.DEFAULT_TYPE):
    await _send_proposal(ALLOWED_USER, context)


async def _send_proposal(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    calendar_events = ""
    try:
        events = await asyncio.to_thread(gcal_.get_today_events)
        calendar_events = gcal_.format_events(events)
    except Exception:
        pass  # calendar is optional — plan without it if unavailable

    try:
        items = await agenda_.generate(planner_, calendar_events)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Agenda generation failed: {e}")
        return

    if not items:
        await context.bot.send_message(chat_id=chat_id, text="No agenda items returned — try again.")
        return

    selected = set(range(len(items)))
    _pending[chat_id] = {"items": items, "selected": selected}

    await context.bot.send_message(
        chat_id=chat_id,
        text=_proposal_text(items, selected),
        parse_mode="HTML",
        reply_markup=_proposal_keyboard(items, selected),
    )


QUIET_START = time(0, 0)   # 00:00
QUIET_END   = time(8, 0)   # 08:00
EVENT_QUIET_END = time(22, 0)  # 22:00


def _in_active_window() -> bool:
    now_t = datetime.now(ZoneInfo("Asia/Jerusalem")).time().replace(second=0, microsecond=0)
    return QUIET_END <= now_t <= EVENT_QUIET_END


async def remind_upcoming(context: ContextTypes.DEFAULT_TYPE):
    if not _in_active_window():
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
        await context.bot.send_message(chat_id=ALLOWED_USER, text=msg, parse_mode="HTML", reply_markup=keyboard)


async def handle_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_answer(update.callback_query)
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass


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
<code>done &lt;N or name&gt;</code> — mark item done
<code>missed &lt;N or name&gt;</code> — mark item missed
<code>add: &lt;text&gt;</code> — add your own agenda item
<code>edit &lt;N&gt; &lt;new text&gt;</code> — edit an agenda item

<b>Review</b>
/digest — AI review of the last 7 days (also runs automatically every Sunday at 20:00)
/metrics — tracked metrics with trend (last 14 days)
/logs — view today's log entries

<b>Context</b>
/context — view and edit your goals, priorities, constraints, projects, principles

<b>Logging</b>
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


_DIGEST_TEMPLATE = Path(__file__).parent / "context" / "templates" / "digest-template.md"
_DIGEST_DIR = Path(__file__).parent / "context" / "digests"


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
        text = await planner_.digest()
        _save_digest(text, label="digest")
        await update.message.reply_text(_digest_to_html(text), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Digest failed: {e}")


async def weekly_digest(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await planner_.digest()
        _save_digest(text, label="weekly-digest")
        await context.bot.send_message(chat_id=ALLOWED_USER, text=f"📋 <b>Weekly digest:</b>\n\n{_digest_to_html(text)}", parse_mode="HTML")
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


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    due = reminders.due_now()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Dismiss", callback_data="remind_dismiss")]])
    for r in due:
        if r.get("auto_log"):
            logs.write("reminder", r["text"])
        await context.bot.send_message(
            chat_id=ALLOWED_USER,
            text=f"⏰ <b>{html.escape(r['text'])}</b>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# --- Entry point ---

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("metrics", cmd_metrics))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("status", cmd_agenda_status))
    app.add_handler(CallbackQueryHandler(handle_proposal_callback, pattern="^pt_"))
    app.add_handler(CallbackQueryHandler(handle_agenda_callback, pattern="^ag_"))
    app.add_handler(CallbackQueryHandler(handle_dismiss, pattern="^remind_dismiss$"))
    app.add_handler(CallbackQueryHandler(handle_reminder_delete, pattern="^rm_del:"))
    app.add_handler(CallbackQueryHandler(handle_context_callback, pattern="^ctx_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_error_handler)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.job_queue.run_daily(
        morning_plan,
        time=time(hour=PLAN_HOUR, minute=PLAN_MINUTE),
        name="morning_plan",
    )
    app.job_queue.run_repeating(remind_upcoming, interval=600, first=60, name="reminders")
    app.job_queue.run_repeating(check_reminders, interval=60, first=10, name="recurring_reminders")
    app.job_queue.run_daily(
        weekly_digest,
        time=time(hour=20, minute=0),
        days=(6,),  # Sunday only
        name="weekly_digest",
    )

    app.run_polling()


if __name__ == "__main__":
    main()

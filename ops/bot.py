import asyncio
import difflib
import html
import os
import re
import tempfile
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import openai
from dotenv import load_dotenv

load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import agenda
import gcal
import planner
import reminders as reminders_mod

TOKEN = os.environ["OPS_BOT_TOKEN"]
ALLOWED_USER = int(os.environ["OPS_CHAT_ID"])
MODEL = os.environ.get("OPS_MODEL", "claude-haiku-4-5-20251001")
PLAN_HOUR = int(os.environ.get("OPS_PLAN_HOUR", "8"))
PLAN_MINUTE = int(os.environ.get("OPS_PLAN_MINUTE", "0"))

cwd = os.getcwd()
LOG_DIR = os.path.expanduser(f"{cwd}/ops/log")
os.makedirs(LOG_DIR, exist_ok=True)

PREFIXES = {
    "insight:": "#insight",
    "hypothesis:": "#hypothesis",
    "checkin": "#checkin",
    "task:": "#task",
    "note:": "#note",
}

# Pending proposal state keyed by chat_id (single-user bot, in-memory is fine)
_pending: dict = {}


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
    lines = [f"📋 <b>Proposed agenda ({html.escape(planner.day_type())}):</b>\n"]
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


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    all_reminders = reminders_mod.load()
    if not all_reminders:
        await update.message.reply_text("No recurring reminders set. Use remind: to add one.")
        return
    rows = []
    for r in all_reminders:
        if r["type"] == "daily":
            label = f"⏰ {r['text']} — daily at {r['time']}"
        else:
            label = f"⏰ {r['text']} — every {r['interval_minutes']} min"
        rows.append([
            InlineKeyboardButton(label, callback_data="noop"),
            InlineKeyboardButton("🗑", callback_data=f"rm_del:{r['id']}"),
        ])
    await update.message.reply_text(
        "⏰ <b>Recurring reminders:</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def handle_reminder_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    reminder_id = query.data.split(":")[1]
    reminders_mod.remove(reminder_id)
    all_reminders = reminders_mod.load()
    if not all_reminders:
        await query.edit_message_text("All recurring reminders removed.")
        return
    rows = []
    for r in all_reminders:
        if r["type"] == "daily":
            label = f"⏰ {r['text']} — daily at {r['time']}"
        else:
            label = f"⏰ {r['text']} — every {r['interval_minutes']} min"
        rows.append([
            InlineKeyboardButton(label, callback_data="noop"),
            InlineKeyboardButton("🗑", callback_data=f"rm_del:{r['id']}"),
        ])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    try:
        events = await asyncio.to_thread(gcal.get_today_events)
        text = gcal.format_events(events)
    except Exception as e:
        text = f"Could not fetch calendar: {e}"
    await update.message.reply_text(f"📅 <b>Today's events:</b>\n{html.escape(text)}", parse_mode="HTML")


async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    open_items = agenda.get_open(LOG_DIR)
    if not open_items:
        await update.message.reply_text("No open agenda items. Use /plan to generate one.")
        return
    rows = []
    for item in open_items:
        label = item["text"] if len(item["text"]) <= 40 else item["text"][:37] + "…"
        rows.append([InlineKeyboardButton(html.escape(label), callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✅ Done", callback_data=f"ag_done:{item['id']}"),
            InlineKeyboardButton("❌ Missed", callback_data=f"ag_missed:{item['id']}"),
        ])
    keyboard = InlineKeyboardMarkup(rows)
    await update.message.reply_text("📋 <b>Open items:</b>", parse_mode="HTML", reply_markup=keyboard)


async def handle_agenda_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "noop":
        return
    action, item_id = data.split(":")[0], int(data.split(":")[1])
    status = "done" if action == "ag_done" else "missed"
    agenda.mark_status(LOG_DIR, item_id, status)

    # refresh the keyboard with remaining open items
    open_items = agenda.get_open(LOG_DIR)
    if not open_items:
        await query.edit_message_text("✅ All items resolved.")
        return
    rows = []
    for item in open_items:
        label = item["text"] if len(item["text"]) <= 40 else item["text"][:37] + "…"
        rows.append([InlineKeyboardButton(html.escape(label), callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✅ Done", callback_data=f"ag_done:{item['id']}"),
            InlineKeyboardButton("❌ Missed", callback_data=f"ag_missed:{item['id']}"),
        ])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))


# --- Proposal callback ---

async def handle_proposal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

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
        await query.answer(f"Send new text for item {idx + 1}:")
        await query.message.reply_text(f"✏️ Send new text for item {idx + 1}:")

    elif data == "pt_no":
        del _pending[chat_id]
        await query.edit_message_text("Proposal discarded.")


def _commit_agenda(texts: list[str], source: str = "llm"):
    items = agenda.accept_items(LOG_DIR, texts, source=source)
    agenda.write_to_markdown(LOG_DIR, items)


# --- Message handler ---

_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

def _normalize(text: str) -> str:
    return " ".join(_NUM_WORDS.get(w, w) for w in text.split())


async def _process_text(text: str, reply) -> None:
    now = datetime.now().strftime("%H:%M")
    lower = _normalize(text.lower()).strip(".,!?;: ")

    # edit N <text> — update agenda item text
    edit_match = re.match(r"^edit\s+(\d+)\s+(.+)$", lower)
    if edit_match:
        item_id = int(edit_match.group(1)) - 1
        new_text = text[edit_match.start(2):]
        agenda.edit_item(LOG_DIR, item_id, new_text)
        await reply(f"✏️ Item {item_id + 1} updated.")
        return

    # done N / missed N — mark by number
    done_match = re.match(r"^(done|missed)\s+(\d+)$", lower)
    if done_match:
        action, n = done_match.group(1), int(done_match.group(2))
        item_id = n - 1
        agenda.mark_status(LOG_DIR, item_id, action)
        icon = "✅" if action == "done" else "❌"
        await reply(f"{icon} Item {n} marked {action}.")
        return

    # done <name> / missed <name> — mark by fuzzy name match
    name_match = re.match(r"^(done|missed)\s+(.+)$", lower)
    if name_match:
        action, query_text = name_match.group(1), name_match.group(2)
        open_items = agenda.get_open(LOG_DIR)
        if open_items:
            item_texts = [i["text"].lower() for i in open_items]
            matches = difflib.get_close_matches(query_text, item_texts, n=1, cutoff=0.3)
            if not matches:
                # fallback: substring match
                matches = [t for t in item_texts if query_text in t or t in query_text]
            if matches:
                item = open_items[item_texts.index(matches[0])]
                agenda.mark_status(LOG_DIR, item["id"], action)
                icon = "✅" if action == "done" else "❌"
                await reply(f"{icon} \"{item['text']}\" marked {action}.")
                return
        await reply(f"Couldn't match \"{query_text}\" to any open agenda item.")
        return

    # event: — create a Google Calendar event
    if lower.startswith("event:"):
        event_text = text[6:].strip()
        await reply("📅 Parsing event…")
        try:
            parsed = await planner.parse_event(event_text)
            if not parsed:
                await reply("Couldn't parse the event. Try: event: Meeting with X tomorrow at 3pm")
                return
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Asia/Jerusalem")
            start_dt = datetime.fromisoformat(
                f"{parsed['date']}T{parsed['start_time']}:00"
            ).replace(tzinfo=tz)
            event = await asyncio.to_thread(
                gcal.create_event,
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

    # remind: — create a recurring reminder
    if lower.startswith("remind:"):
        reminder_text = text[7:].strip()
        await reply("⏰ Parsing reminder…")
        try:
            parsed = await planner.parse_reminder(reminder_text)
            if not parsed:
                await reply("Couldn't parse the reminder. Try: remind: eat lunch at 13:00 or remind: drink water every 60 minutes")
                return
            entry = reminders_mod.add(**parsed)
            if entry["type"] == "daily":
                await reply(f"⏰ Reminder set: <b>{html.escape(entry['text'])}</b> daily at {entry['time']}", )
            else:
                await reply(f"⏰ Reminder set: <b>{html.escape(entry['text'])}</b> every {entry['interval_minutes']} min")
        except Exception as e:
            await reply(f"Failed to set reminder: {e}")
        return

    # add: — user adds their own agenda item
    if lower.startswith("add:"):
        item_text = text[4:].strip()
        _commit_agenda([item_text], source="user")
        await reply(f"Added to agenda: {item_text}")
        return

    # standard log entry
    tag = "#log"
    content = text
    for prefix, t in PREFIXES.items():
        if lower.startswith(prefix):
            tag = t
            content = text[len(prefix):].strip()
            break

    log_file = os.path.join(LOG_DIR, f"{datetime.now().date()}.md")
    with open(log_file, "a") as f:
        f.write(f"\n## {now} {tag}\n{content}\n")

    await reply(f"Logged {tag} ✓")


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

    await _process_text(text, update.message.reply_text)


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
    await _process_text(text, update.message.reply_text)


# --- Scheduled morning plan ---

async def morning_plan(context: ContextTypes.DEFAULT_TYPE):
    await _send_proposal(ALLOWED_USER, context)


async def _send_proposal(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    calendar_events = ""
    try:
        events = await asyncio.to_thread(gcal.get_today_events)
        calendar_events = gcal.format_events(events)
    except Exception:
        pass  # calendar is optional — plan without it if unavailable

    existing = agenda.load(LOG_DIR)["items"]
    existing_summary = ""
    if existing:
        done = [i["text"] for i in existing if i["status"] in ("done", "missed")]
        open_ = [i["text"] for i in existing if i["status"] == "open"]
        parts = []
        if done:
            parts.append("Already completed/missed:\n" + "\n".join(f"- {t}" for t in done))
        if open_:
            parts.append("Still open:\n" + "\n".join(f"- {t}" for t in open_))
        existing_summary = "\n\n".join(parts)

    try:
        items = await planner.propose(MODEL, LOG_DIR, calendar_events, existing_summary)
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


async def remind_upcoming(context: ContextTypes.DEFAULT_TYPE):
    try:
        events = await asyncio.to_thread(gcal.get_upcoming_events, within_minutes=15)
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
            from zoneinfo import ZoneInfo
            t = datetime.fromisoformat(start).astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
            msg = f"⏰ Reminder: <b>{html.escape(summary)}</b> at {t}"
        else:
            msg = f"⏰ Reminder: <b>{html.escape(summary)}</b> starting soon"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Dismiss", callback_data="remind_dismiss")]])
        await context.bot.send_message(chat_id=ALLOWED_USER, text=msg, parse_mode="HTML", reply_markup=keyboard)


async def handle_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)


HELP_TEXT = """<b>Commands</b>
/plan — propose today's agenda (also runs at 06:00)
/agenda — show open items with Done/Missed buttons
/events — show today's calendar
/reminders — list recurring reminders (with delete)
/help — show this message

<b>Messages</b>
<code>done &lt;N or name&gt;</code> — mark agenda item done
<code>missed &lt;N or name&gt;</code> — mark agenda item missed
<code>add: &lt;text&gt;</code> — add your own agenda item
<code>edit &lt;N&gt; &lt;text&gt;</code> — edit an agenda item
<code>event: &lt;description&gt;</code> — create a Google Calendar event
<code>remind: &lt;description&gt;</code> — set a recurring reminder
<code>note: / insight: / task: / hypothesis: / checkin</code> — log an entry

Voice notes are transcribed automatically."""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    due = reminders_mod.due_now(reminders_mod.load())
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Dismiss", callback_data="remind_dismiss")]])
    for r in due:
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
    app.add_handler(CallbackQueryHandler(handle_proposal_callback, pattern="^pt_"))
    app.add_handler(CallbackQueryHandler(handle_agenda_callback, pattern="^ag_|^noop$"))
    app.add_handler(CallbackQueryHandler(handle_dismiss, pattern="^remind_dismiss$"))
    app.add_handler(CallbackQueryHandler(handle_reminder_delete, pattern="^rm_del:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.job_queue.run_daily(
        morning_plan,
        time=time(hour=PLAN_HOUR, minute=PLAN_MINUTE),
        name="morning_plan",
    )
    app.job_queue.run_repeating(remind_upcoming, interval=600, first=60, name="reminders")
    app.job_queue.run_repeating(check_reminders, interval=60, first=10, name="recurring_reminders")

    app.run_polling()


if __name__ == "__main__":
    main()

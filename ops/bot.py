import asyncio
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
_reminded: set = set()  # event IDs already sent a reminder this session


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
    lines = [f"{item['id'] + 1}. {html.escape(item['text'])}" for item in open_items]
    await update.message.reply_text(
        "📋 <b>Open items:</b>\n" + "\n".join(lines), parse_mode="HTML"
    )


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

    # done N / missed N — mark agenda item
    done_match = re.match(r"^(done|missed)\s+(\d+)$", lower)
    if done_match:
        action, n = done_match.group(1), int(done_match.group(2))
        item_id = n - 1
        agenda.mark_status(LOG_DIR, item_id, action)
        icon = "✅" if action == "done" else "❌"
        await reply(f"{icon} Item {n} marked {action}.")
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

    try:
        items = await planner.propose(MODEL, LOG_DIR, calendar_events)
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
    for event in events:
        eid = event.get("id")
        if eid in _reminded:
            continue
        _reminded.add(eid)
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


# --- Entry point ---

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CallbackQueryHandler(handle_proposal_callback, pattern="^pt_"))
    app.add_handler(CallbackQueryHandler(handle_dismiss, pattern="^remind_dismiss$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.job_queue.run_daily(
        morning_plan,
        time=time(hour=PLAN_HOUR, minute=PLAN_MINUTE),
        name="morning_plan",
    )
    app.job_queue.run_repeating(remind_upcoming, interval=600, first=60, name="reminders")

    app.run_polling()


if __name__ == "__main__":
    main()

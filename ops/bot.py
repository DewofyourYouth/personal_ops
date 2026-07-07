import asyncio
import html
import logging
import os
import re
import sys
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import scheduling
from agenda import Agenda
from agenda_handlers import AgendaHandlers
from agenda_queue import AgendaQueue
from backlog import Backlog
from baseline_tracker import Baseline
from bot_constants import HELP_INTRO, HELP_SECTIONS, HELP_TEXT  # noqa: F401
from config import Config
from context import Context
from digest import DigestHandlers
from gcal import GCal
from logs import Logs
from media import send_startup_animation
from planner import Planner
from plugins import build_plugins, collect_jobs
from reminder_handlers import ReminderHandlers
from reminders import Reminders
from shabbat import Shabbat
from status_handlers import StatusHandlers
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
from text_router import (
    ENERGY_OPTIONS,
    MOOD_OPTIONS,
    TextRouter,
    _mood_energy_keyboard,
    _parse_queue_date,
)
from tg_common import safe_answer
from weight import Weight

# Single per-instance config object: identity, storage path, and tunables come
# from here rather than from scattered env reads or getcwd(). The globals below
# are kept as thin aliases so the rest of bot.py is unchanged.
# Reflective outputs (digests, agenda proposals, hypothesis eval, feedback) run on Sonnet —
# Haiku ignores nuanced tone restraint (no coda, no moralizing, no directives) and falls back
# on a generic "church lady" register. Cheap structured parsing (reminders/events/food) stays
# on Haiku, hardcoded in those methods.
config = Config.from_env()
TOKEN = config.bot_token
ALLOWED_USER = config.allowed_user
MODEL = config.model
PLAN_HOUR = config.plan_hour
PLAN_MINUTE = config.plan_minute
LOG_DIR = str(config.data_dir)

# Global bot reference — set in post_init once the Application starts
_bot = None

# Running scheduler instance, created in _post_init via the scheduling layer.
_scheduler = None

# --- Service instances ---
logs = Logs(LOG_DIR)
agenda_ = Agenda(LOG_DIR)
queue_ = AgendaQueue(LOG_DIR)
backlog_ = Backlog(LOG_DIR)
reminders = Reminders()
gcal_ = GCal()
context_ = Context()
planner_ = Planner(MODEL, logs, context_)
baseline_ = Baseline(LOG_DIR)
weight_ = Weight(logs.db)
shabbat_ = Shabbat(LOG_DIR)


# Feature handler instances, created in main() once app.bot exists.
agenda_feature: "AgendaHandlers" = None  # type: ignore[assignment]
router: "TextRouter" = None  # type: ignore[assignment]
digest_feature: "DigestHandlers" = None  # type: ignore[assignment]
reminders_feature: "ReminderHandlers" = None  # type: ignore[assignment]
status_feature: "StatusHandlers" = None  # type: ignore[assignment]
plugins: list = []  # built in main(); _post_init reads their scheduled jobs

# In-memory conversation state keyed by chat_id (single-user bot, in-memory is fine).
# The candle/reminder-time/voice flows keep their state on the TextRouter; the
# reminder-edit flow keeps its state on the ReminderHandlers instance.
_awaiting_context: dict = {}  # chat_id -> filename waiting for new content
_awaiting_queue_day: dict = {}  # chat_id -> {"step": "bl_day", "data": {...}} backlog→queue reply


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        return  # transient connectivity blips — genuinely noisy, swallow
    if isinstance(context.error, BadRequest):
        # Malformed reply (bad HTML, unchanged edit, GIF via wrong method, etc.).
        # For user-initiated commands/messages, tell them it failed so the bot doesn't
        # look like it silently ignored them. Background sends (scheduled messages)
        # have no update to reply to.
        logging.getLogger(__name__).warning(
            "BadRequest while handling update: %s | update=%s", context.error, update
        )
        try:
            if isinstance(update, Update) and update.effective_chat and update.message:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⚠️ Couldn't send that response (formatting error). Check /logs or try again.\n({type(context.error).__name__}: {context.error})",
                )
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to notify user about BadRequest"
            )
        return
    # Never fail silently on a real error: log it AND tell the user their
    # message wasn't handled, so a dropped entry can't disappear unnoticed.
    logging.getLogger(__name__).exception(
        "Unhandled error processing update", exc_info=context.error
    )
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Something went wrong handling that — it was NOT saved. Please resend.\n({type(context.error).__name__}: {context.error})",
            )
    except Exception:
        logging.getLogger(__name__).exception("Failed to notify user about error")


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


# --- Command handlers ---


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    try:
        events = await asyncio.to_thread(gcal_.get_today_events)
        text = gcal_.format_events(events)
    except Exception as e:
        text = f"Could not fetch calendar: {e}"
    await update.message.reply_text(
        f"📅 <b>Today's events:</b>\n{html.escape(text)}", parse_mode="HTML"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # intercept edit reply for pending proposal (owned by the agenda feature)
    if await agenda_feature.try_handle_proposal_edit(update):
        return

    # intercept voice transcript edit (owned by the text router)
    if await router.try_handle_voice_edit(update):
        return

    # intercept reminder edit reply (owned by the reminders feature)
    if await reminders_feature.try_handle_edit_reply(update):
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

    # intercept candle lighting time (owned by the text router)
    if await router.try_handle_candle_reply(update):
        return

    # intercept backlog→queue day reply
    if chat_id in _awaiting_queue_day:
        state = _awaiting_queue_day.pop(chat_id)
        target = _parse_queue_date(text.strip())
        item_id = state["data"]["item_id"]
        item_text = state["data"]["text"]
        if not target:
            await update.message.reply_text(
                "Couldn't parse that day. Try: Sunday, Monday, tomorrow…"
            )
            return
        queue_.add(item_text, target)
        backlog_.remove(item_id)
        await update.message.reply_text(
            f"📅 Queued for {target.strftime('%A %b %d')}: {html.escape(item_text)}",
            parse_mode="HTML",
        )
        return

    # intercept time reply for pending reminder (owned by the text router)
    if await router.try_handle_time_reply(update):
        return

    # intercept a food-estimate portion correction (owned by the text router)
    if await router.try_handle_food_adjust(update):
        return

    # Plugin-owned natural-language captures, before the central router logs the text.
    for plugin in plugins:
        try_handle_text = getattr(plugin, "try_handle_text", None)
        if try_handle_text and await try_handle_text(update, text):
            return

    await router.process_text(text, update.message.reply_text, chat_id=chat_id)


# --- Scheduled morning plan ---


async def morning_plan():
    if shabbat_.quiet_now():
        return
    # The "plan" sticker now fires inside send_proposal (so manual /plan shows it too).
    await agenda_feature.send_proposal(ALLOWED_USER)
    # Friday: ask for candle lighting time
    if datetime.now(ZoneInfo("Asia/Jerusalem")).weekday() == 4:
        if not shabbat_.load_candle_lighting():
            await _bot.send_message(
                chat_id=ALLOWED_USER,
                text="🕯️ What time is candle lighting today?",
            )
            router.expect_candle_time(ALLOWED_USER)


async def remind_upcoming():
    if shabbat_.quiet_now() or not shabbat_.in_active_window():
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
            t = (
                datetime.fromisoformat(start)
                .astimezone(ZoneInfo("Asia/Jerusalem"))
                .strftime("%H:%M")
            )
            msg = f"⏰ Reminder: <b>{html.escape(summary)}</b> at {t}"
        else:
            msg = f"⏰ Reminder: <b>{html.escape(summary)}</b> starting soon"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✓ Dismiss", callback_data="remind_dismiss")]]
        )
        await _bot.send_message(
            chat_id=ALLOWED_USER, text=msg, parse_mode="HTML", reply_markup=keyboard
        )


async def handle_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    is_checkin = query.data == "remind_dismiss_c"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    if is_checkin:
        logs.write("checkin", "reminder dismissed")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="👋 How are you feeling right now?",
            reply_markup=_mood_energy_keyboard(),
        )


def _help_menu_keyboard() -> InlineKeyboardMarkup:
    keys = list(HELP_SECTIONS)
    rows = [
        [
            InlineKeyboardButton(HELP_SECTIONS[k][0], callback_data=f"help:{k}")
            for k in keys[i : i + 2]
        ]
        for i in range(0, len(keys), 2)
    ]
    return InlineKeyboardMarkup(rows)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(
        HELP_INTRO, parse_mode="HTML", reply_markup=_help_menu_keyboard()
    )


async def handle_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    key = query.data.split(":", 1)[1]
    if key == "back":
        await query.edit_message_text(
            HELP_INTRO, parse_mode="HTML", reply_markup=_help_menu_keyboard()
        )
        return
    section = HELP_SECTIONS.get(key)
    if not section:
        return
    title, body = section
    back = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ Back", callback_data="help:back")]]
    )
    await query.edit_message_text(
        f"<b>{title}</b>\n\n{body}", parse_mode="HTML", reply_markup=back
    )


# Scheduler wrappers: the digest logic lives in DigestHandlers, but the persistent
# job store needs picklable module-level callables, so these thin functions delegate
# to the feature instance built in main().
async def scheduled_daily_digest():
    await digest_feature.run_scheduled_daily()


async def weekly_digest():
    await digest_feature.run_weekly()


async def cmd_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    data = logs.load_metrics(days=14)
    if not data:
        await update.message.reply_text(
            "No metrics logged yet. Use: metric: steps 8000"
        )
        return
    lines = ["📊 <b>Metrics (last 14 days):</b>\n"]
    for key, entries in sorted(data.items()):
        numeric = [v for _, v in entries if isinstance(v, (int, float))]
        trend = ""
        if len(numeric) >= 3:
            if numeric[-1] > numeric[-3]:
                trend = " ↑"
            elif numeric[-1] < numeric[-3]:
                trend = " ↓"
            else:
                trend = " →"
        avg = f" | avg {sum(numeric) / len(numeric):.1f}" if len(numeric) > 1 else ""
        recent = ", ".join(str(v) for _, v in entries[-5:])
        lines.append(f"<b>{key}</b>: {recent}{avg}{trend}")

    tod = logs.mood_energy_by_time_of_day(days=14)
    if tod:
        lines.append("\n🕐 <b>Mood/energy by time of day:</b>")
        lines.append(
            "<table><tr><th>Time</th><th>Mood</th><th>Energy</th><th>n</th></tr>"
        )
        for label in ("late night", "morning", "afternoon", "evening"):
            if label not in tod:
                continue
            b = tod[label]
            mood = b["mood_avg"] if b["mood_avg"] is not None else "—"
            energy = b["energy_avg"] if b["energy_avg"] is not None else "—"
            lines.append(
                f"<tr><td>{label}</td><td>{mood}</td><td>{energy}</td><td>{b['n']}</td></tr>"
            )
        lines.append("</table>")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await update.message.reply_text(text, parse_mode="HTML")


# Drug names to strip from shareable /weight output (the data/digests keep them).
_PRIVATE_TERMS = re.compile(r"\s*\b(?:wegovy|semaglutide|ozempic)\b", re.IGNORECASE)


def _scrub_private(text: str) -> str:
    """Remove medication names so /weight output can be shown to others."""
    return _PRIVATE_TERMS.sub("", text)


async def cmd_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    text = weight_.format_for_telegram()
    try:
        synopsis = await planner_.weight_synopsis_cached()
        if synopsis:
            text = f"📝 {html.escape(synopsis)}\n\n{text}"
    except Exception:
        pass  # the figures stand on their own if the synopsis call fails
    # Privacy scrub: the /weight output is shareable, so strip the drug name (the synopsis
    # may name it). Digests and stored data keep it — only this command's output is scrubbed.
    text = _scrub_private(text)
    await update.message.reply_text(text, parse_mode="HTML")

    # Chart as a follow-up photo (rendering is offloaded so the bot loop isn't blocked).
    try:
        png = await asyncio.to_thread(weight_.chart_png)
        if png:
            await update.message.reply_photo(photo=png)
    except Exception:
        logging.getLogger(__name__).exception("Weight chart render failed")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    messages = logs.format_today_for_telegram()
    if not messages:
        await update.message.reply_text("No log entries today.")
        return
    for text in messages:
        await update.message.reply_text(text, parse_mode="HTML")


async def cmd_directives(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    # Read the new `directive` tag plus any legacy `values` rows (not yet backfilled),
    # merged chronologically so the evolution still reads top-to-bottom.
    rows = logs.db.entries_by_tag("directive") + logs.db.entries_by_tag("values")
    rows.sort(key=lambda r: r["ts"])
    if not rows:
        await update.message.reply_text(
            "No directives logged yet. Use <code>directive: ...</code> to declare a standing "
            "instruction to the app (e.g. <i>directive: more is not always better</i>).",
            parse_mode="HTML",
        )
        return
    # Chronological, grouped by date, so the evolution reads top-to-bottom
    lines = ["🧭 <b>Directives</b> — standing instructions to the app:\n"]
    last_date = None
    for r in rows:
        if r["date"] != last_date:
            lines.append(f"\n<b>{r['date']}</b>")
            last_date = r["date"]
        t = r["ts"][11:16]
        lines.append(f"<code>{t}</code> {html.escape(r['content'])}")
    text = "\n".join(lines)
    # If too long, show the most recent portion (keep the latest evolution visible)
    if len(text) > 4000:
        text = "🧭 <b>Directives</b> (most recent):\n" + "\n".join(lines[-40:])
        text = text[:4000]
    await update.message.reply_text(text, parse_mode="HTML")


async def handle_mood_energy_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split(":")  # me_mood:😊:good  or  me_energy:⚡:high
    kind, _, value = parts[0][3:], parts[1], parts[2]  # strip "me_"; ignore emoji
    _mood_scores = {"great": 5, "good": 4, "okay": 3, "low": 2, "bad": 1}
    _energy_scores = {"high": 3, "okay": 2, "drained": 1}
    numeric = _mood_scores.get(value) if kind == "mood" else _energy_scores.get(value)
    logs.write_metric(kind, numeric if numeric is not None else value)
    # rebuild keyboard with this selection locked, other row still active
    locked_mood = value if kind == "mood" else ""
    locked_energy = value if kind == "energy" else ""
    # carry over previously locked value from existing keyboard if present
    for row in query.message.reply_markup.inline_keyboard or []:
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


def _backlog_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    # Group by domain, each under a non-interactive header row. "General" sorts last.
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item.get("domain") or "General", []).append(item)
    rows = []
    for domain in sorted(groups, key=lambda d: (d == "General", d.lower())):
        rows.append([InlineKeyboardButton(f"— {domain} —", callback_data="noop")])
        for item in groups[domain]:
            short = item["text"] if len(item["text"]) <= 30 else item["text"][:27] + "…"
            rows.append(
                [
                    InlineKeyboardButton(f"📋 {short}", callback_data="noop"),
                    InlineKeyboardButton("📅", callback_data=f"bl_queue:{item['id']}"),
                    InlineKeyboardButton("🗑", callback_data=f"bl_del:{item['id']}"),
                ]
            )
    return InlineKeyboardMarkup(rows)


async def cmd_backlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    items = backlog_.load()
    if not items:
        await update.message.reply_text(
            "Backlog is empty. Use <code>backlog: idea or task</code> to add.",
            parse_mode="HTML",
        )
        return
    # Lazily classify any items without a domain (new adds, or pre-domain items), reusing
    # the domains already in play, then persist so it's a one-time cost per item.
    undomained = [it for it in items if not it.get("domain")]
    if undomained:
        existing = sorted({it["domain"] for it in items if it.get("domain")})
        labels = await planner_.classify_backlog_domains(
            [it["text"] for it in undomained], existing
        )
        for it, label in zip(undomained, labels):
            it["domain"] = label
        backlog_.save(items)
    text = f"📋 <b>Backlog ({len(items)} items):</b>"
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=_backlog_keyboard(items)
    )


async def handle_backlog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    action, item_id = query.data.split(":", 1)

    async def render_remaining(message: str | None = None) -> None:
        """Re-show the backlog after a mutation. With a message, replace the text
        (keeping the buttons if anything's left); without one, just refresh the
        buttons in place."""
        items = backlog_.load()
        if not items:
            await query.edit_message_text(
                message or "Backlog is empty.", parse_mode="HTML"
            )
        elif message:
            await query.edit_message_text(
                message, parse_mode="HTML", reply_markup=_backlog_keyboard(items)
            )
        else:
            await query.edit_message_reply_markup(reply_markup=_backlog_keyboard(items))

    match action:
        case "bl_del":
            backlog_.remove(item_id)
            await render_remaining()

        case "bl_queue":
            item = backlog_.get(item_id)
            if not item:
                await query.edit_message_text("Item not found.")
                return
            _awaiting_queue_day[query.message.chat_id] = {
                "step": "bl_day",
                "data": {"item_id": item_id, "text": item["text"]},
            }
            await query.edit_message_text(
                f"📅 Queue <b>{html.escape(item['text'])}</b> for which day?",
                parse_mode="HTML",
            )

        case "bl_confirm":
            # item_id encodes "<backlog_id>:<day_str>"
            backlog_id, day_str = item_id.split(":", 1)
            item = backlog_.get(backlog_id)
            target = _parse_queue_date(day_str) if item else None
            if not item or not target:
                return
            queue_.add(item["text"], target)
            backlog_.remove(backlog_id)
            await render_remaining(
                f"📅 Queued for {target.strftime('%A %b %d')}: {html.escape(item['text'])}"
            )


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    rows = [
        [
            InlineKeyboardButton(
                f.replace(".md", "").title(), callback_data=f"ctx_view:{f}"
            )
        ]
        for f in context_.files()
    ]
    await update.message.reply_text(
        "📁 <b>Context files:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_context_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    data = query.data

    if data.startswith("ctx_view:"):
        fname = data.split(":", 1)[1]
        content = context_.read(fname) or "(empty)"
        title = fname.replace(".md", "").title()
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✏️ Edit", callback_data=f"ctx_edit:{fname}"),
                    InlineKeyboardButton("« Back", callback_data="ctx_back"),
                ]
            ]
        )
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
            [
                InlineKeyboardButton(
                    f.replace(".md", "").title(), callback_data=f"ctx_view:{f}"
                )
            ]
            for f in context_.files()
        ]
        await query.edit_message_text(
            "📁 <b>Context files:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )


# Scheduler wrapper: the firing logic lives in ReminderHandlers, but the
# persistent job store needs a picklable module-level callable, so this thin
# function delegates to the feature instance built in main().
async def check_reminders():
    await reminders_feature.run_due_check()


# --- APScheduler lifecycle ---


async def _post_init(application):
    global _bot
    _bot = application.bot
    # Self-heal: replay any JSONL readings the DB missed (e.g. dropped by a
    # transient lock before the WAL/busy-timeout fix). Idempotent.
    recovered = 0
    try:
        recovered = logs.sync_jsonl_to_db()
        if recovered:
            logging.getLogger(__name__).info(
                "Recovered %d log row(s) from JSONL on startup", recovered
            )
    except Exception:
        logging.getLogger(__name__).exception("JSONL→DB sync on startup failed")
    # Tell the user we just (re)started, so a redeploy can't silently eat a
    # message sent during the restart window — they know to resend.
    try:
        note = f"\n♻️ Recovered {recovered} log row(s)." if recovered else ""
        await application.bot.send_message(
            chat_id=ALLOWED_USER,
            text=f"🔄 Bot back online. If you sent anything in the last minute, please resend it.{note}",
        )
        await send_startup_animation(application.bot, ALLOWED_USER)
    except Exception:
        logging.getLogger(__name__).exception("Failed to send back-online ping")
    global _scheduler
    _scheduler = scheduling.start(
        LOG_DIR,
        {
            "morning_plan": morning_plan,
            "remind_upcoming": remind_upcoming,
            "check_reminders": check_reminders,
            "daily_digest": scheduled_daily_digest,
            "weekly_digest": weekly_digest,
        },
        plan_hour=PLAN_HOUR,
        plan_minute=PLAN_MINUTE,
        extra_jobs=collect_jobs(plugins),
    )


async def _post_shutdown(application):
    scheduling.shutdown(_scheduler)


# --- Entry point ---


def main():
    # Log to stdout so `docker logs` / journald capture it. Honour LOG_LEVEL
    # (default INFO); quiet the chatty httpx request log to WARNING.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("Starting personal_ops bot")

    app = (
        Application.builder()
        .token(TOKEN)
        # Default 5s timeouts are too short for container/VPS startup under slow network.
        # Generous timeouts so a transient Telegram-API slowdown doesn't crash bootstrap.
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Feature handlers (own their commands + callbacks via register()).
    global agenda_feature
    agenda_feature = AgendaHandlers(
        app.bot, agenda_, queue_, gcal_, planner_, logs, ALLOWED_USER
    )
    agenda_feature.register(app)

    # Plugins (tracking-domain feature classes) self-register via the registry.
    global plugins
    services = SimpleNamespace(
        logs=logs,
        context=context_,
        planner=planner_,
        gcal=gcal_,
        queue=queue_,
        agenda=agenda_,
        backlog=backlog_,
        baseline=baseline_,
        reminders=reminders,
        allowed_user=ALLOWED_USER,
    )
    plugins = build_plugins(app.bot, services)
    for plugin in plugins:
        plugin.register(app)

    # Central inbound-message router: owns process_text + the candle/reminder-time/
    # voice flows. It commits user-added agenda items through the agenda feature.
    global router
    router = TextRouter(app.bot, services, shabbat_, ALLOWED_USER)
    router.agenda_feature = agenda_feature
    # Hand the grocery plugin to the router so confirmed voice transcripts opening
    # with "grocery"/"groceries" route into the list (the plugin owns the logic).
    router.grocery = next((p for p in plugins if hasattr(p, "handle_voice_text")), None)
    router.plugins = plugins
    router.register(app)

    # Digest feature (daily + weekly reviews). Its scheduled runs are wrapped by
    # the module-level scheduled_daily_digest / weekly_digest for the job store.
    global digest_feature
    digest_feature = DigestHandlers(
        app.bot, planner_, baseline_, logs, context_, shabbat_, ALLOWED_USER
    )
    digest_feature.register(app)

    # Reminders feature (list / edit / delete UI + the due-reminder firing job,
    # wrapped by the module-level check_reminders for the job store).
    global reminders_feature
    reminders_feature = ReminderHandlers(
        app.bot, reminders, logs, shabbat_, ALLOWED_USER
    )
    reminders_feature.register(app)

    # Status snapshot (/status): a cross-cutting dashboard that composes the agenda
    # feature, the habit plugin, the calendar, and a planner synopsis. The habit
    # plugin is found by duck-typing (same pattern as router.grocery above).
    global status_feature
    status_feature = StatusHandlers(
        app.bot, agenda_feature, gcal_, planner_, shabbat_, ALLOWED_USER
    )
    status_feature.habits = next(
        (p for p in plugins if hasattr(p, "pending_today")), None
    )
    status_feature.register(app)

    # app.add_handler(CommandHandler(, cmd_help))
    app.add_handler(CallbackQueryHandler(handle_help_callback, pattern="^help:"))
    app.add_handler(CommandHandler({"help", "n", "nav"}, cmd_help))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler({"logs", "l"}, cmd_logs))
    app.add_handler(CommandHandler({"metrics", "m"}, cmd_metrics))
    app.add_handler(CommandHandler({"weight", "w"}, cmd_weight))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler({"backlog", "b"}, cmd_backlog))
    app.add_handler(
        CommandHandler({"directives", "directive", "values", "v"}, cmd_directives)
    )
    app.add_handler(CallbackQueryHandler(handle_backlog_callback, pattern="^bl_"))
    app.add_handler(CallbackQueryHandler(handle_dismiss, pattern="^remind_dismiss"))
    app.add_handler(CallbackQueryHandler(handle_context_callback, pattern="^ctx_"))
    app.add_handler(CallbackQueryHandler(handle_mood_energy_callback, pattern="^me_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()

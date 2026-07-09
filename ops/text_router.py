"""Text router — the central inbound-message dispatcher.

A feature class (same shape as the other handlers): built with the bot and the
domain services it needs, with the conversation state it owns kept on the
instance rather than in module globals. `process_text` parses a free-text or
transcribed message, decides which command/log it is, and acts.

`bot.py`'s `handle_message` stays the composition point that knows about every
feature's pending-reply state; it delegates the candle/reminder-time/voice flows
and the final fall-through to the methods here. Voice intake (transcription +
the confirm/edit loop) self-registers via `register(app)`.

The small parsing helpers (`_parse_time`, `_parse_queue_date`, `_normalize`) and
the mood/energy keyboard are module-level so `bot.py` can reuse them without a
circular import (this module never imports bot.py).
"""

import asyncio
import difflib
import html
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot_constants import PREFIXES
from habit_handlers import exact_habit_match, match_habit
from llm import classify_entry, parse_queue_entry, transcribe_with_language_detection
from media import send_sticker
from tg_common import (
    encourage,
    inline_keyboard_markup,
    inline_keyboard_rows,
    safe_answer,
)


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

_NUM_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}

_UNICODE_JUNK = re.compile("[​-‏‪-‮⁠-⁤﻿]+")

# A "structured" nutrition entry already carries a calorie figure AND a macro breakdown
# (e.g. "chicken bowl — 550 kcal, 40g protein"). Requiring BOTH keeps offhand mentions
# ("burned 500 calories on my walk" — a checkin) out of #food. Used by the rules-first
# pass to tag these #food without an LLM call.
_KCAL_RE = re.compile(r"\d+\s*k?cal(?:ories)?\b", re.IGNORECASE)
_MACRO_RE = re.compile(
    r"\d+\s*g(?:rams)?\s*(?:of\s+)?(?:protein|fat|carb)", re.IGNORECASE
)


def _is_nutrition_breakdown(text: str) -> bool:
    return bool(_KCAL_RE.search(text) and _MACRO_RE.search(text))


# An explicitly stated agenda destination ("add X to my agenda", "put X on the agenda").
# The classifier only extracts an action TYPE (→ #task) and silently drops the stated
# destination, so the item never reaches /agenda — this rules-first match routes it there.
_AGENDA_DEST_RE = re.compile(
    r"\b(?:on|to|in(?:to)?)\s+(?:my|the)\s+agenda\b", re.IGNORECASE
)


def _extract_agenda_item(text: str) -> str:
    """Pull the item out of a 'add X to my agenda' utterance.

    Takes everything before the '… to/on my agenda' phrase and strips a leading
    imperative verb, so 'Add goal reflection to my agenda and …' → 'goal reflection'.
    """
    m = re.search(
        r"^(.*?)\s+(?:on|to|in(?:to)?)\s+(?:my|the)\s+agenda\b",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    item = (m.group(1) if m else text).strip()
    item = re.sub(
        r"^(?:please\s+)?(?:add|put|note|log)\s+", "", item, flags=re.IGNORECASE
    ).strip()
    return item.strip(" .,:;-")


def _parse_metric_body(rest: str) -> tuple[str, float, str, str] | None:
    """Parse the body of a `metric(s):` entry into (key, value, unit, raw_val).

    Handles key/value in either order and tolerates a filler word between the prefix
    and the key ("yesterday's steps 7095" → steps=7095). Returns None if no numeric
    value is present. The key is the alphabetic word adjacent to the number (the one
    before it when present, else the one after).
    """
    rest = rest.strip()
    # A unit is only the letters glued directly to the number ("92.9kg"); a word after a
    # space ("8000 steps") is the key, not a unit.
    num_m = re.search(r"(\d[\d.]*)([A-Za-z%]*)", rest)
    if not num_m:
        return None
    value = float(num_m.group(1))
    unit = num_m.group(2)
    raw_val = num_m.group(0).strip()
    before = re.findall(r"[A-Za-z_][A-Za-z_-]*", rest[: num_m.start()])
    after = re.findall(r"[A-Za-z_][A-Za-z_-]*", rest[num_m.end() :])
    key_word = before[-1] if before else (after[0] if after else None)
    if key_word is None:
        return None
    return key_word.lower().replace("-", "_"), value, unit, raw_val


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
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"(\d{1,2})\s*(am|pm)", text)
    if m:
        h = int(m.group(1))
        if m.group(2) == "pm" and h != 12:
            h += 12
        elif m.group(2) == "am" and h == 12:
            h = 0
        return f"{h:02d}:00"
    m = re.match(r"^(\d{1,2})$", text)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


def _parse_queue_date(day_str: str):
    from datetime import date as _date, timedelta as _td

    today = _date.today()
    day_str = day_str.strip().lower()
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
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


_BACKDATE_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}


def _parse_backdate(text: str) -> tuple[date | None, str]:
    """Pull a leading past-date token off `text` for the /backdate command.

    Returns (resolved_date, remaining_entry_text). Recognises ISO dates, "yesterday",
    "today", "-N" / "N days ago", and weekday names ("friday" / "last friday" → the most
    recent past occurrence). Returns (None, text) when no date is recognised or it would
    resolve to the future — backdating only ever points at today or earlier.
    """
    s = text.strip()
    low = s.lower()
    today = date.today()

    def past_weekday(num: int) -> date:
        return today - timedelta(days=(today.weekday() - num) % 7)

    def iso(token: str) -> date | None:
        try:
            return date.fromisoformat(token)
        except ValueError:
            return None

    def weekday(token: str) -> date | None:
        return (
            past_weekday(_BACKDATE_WEEKDAYS[token])
            if token in _BACKDATE_WEEKDAYS
            else None
        )

    rules = [
        (r"(\d{4}-\d{2}-\d{2})", lambda m: iso(m.group(1))),
        (r"(\d+)\s+days?\s+ago", lambda m: today - timedelta(days=int(m.group(1)))),
        (r"-(\d+)", lambda m: today - timedelta(days=int(m.group(1)))),
        (r"yesterday|yest", lambda m: today - timedelta(days=1)),
        (r"today", lambda m: today),
        (r"last\s+(\w+)", lambda m: weekday(m.group(1))),
        (r"(\w+)", lambda m: weekday(m.group(1))),
    ]
    for pattern, resolve in rules:
        m = re.match(pattern, low)
        if not m:
            continue
        d = resolve(m)
        if d is None:
            continue
        if d > today:
            return None, s
        return d, s[m.end() :].strip()
    return None, s


class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, skipping <script>/<style> contents."""

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and (t := data.strip()):
            self.parts.append(t)


def _html_to_text(markup: str) -> str:
    """Best-effort HTML → readable text (stdlib only; no bs4 dependency)."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
    except Exception:
        pass
    return "\n".join(parser.parts)


MOOD_OPTIONS = [
    ("😄", "great"),
    ("😊", "good"),
    ("😐", "okay"),
    ("😕", "low"),
    ("😞", "bad"),
]
ENERGY_OPTIONS = [("⚡", "high"), ("🔋", "okay"), ("🪫", "drained")]


def _mood_energy_keyboard(
    locked_mood: str = "", locked_energy: str = ""
) -> InlineKeyboardMarkup:
    mood_row = [
        InlineKeyboardButton(
            f"✅ {e} {v}" if locked_mood == v else f"{e} {v}",
            callback_data="noop" if locked_mood == v else f"me_mood:{e}:{v}",
        )
        for e, v in MOOD_OPTIONS
    ]
    energy_row = [
        InlineKeyboardButton(
            f"✅ {e} {v}" if locked_energy == v else f"{e} {v}",
            callback_data="noop" if locked_energy == v else f"me_energy:{e}:{v}",
        )
        for e, v in ENERGY_OPTIONS
    ]
    return inline_keyboard_markup([mood_row, energy_row])


def _food_keyboard() -> InlineKeyboardMarkup:
    return inline_keyboard_markup(
        [
            [
                InlineKeyboardButton("✅ Log it", callback_data="food_confirm"),
                InlineKeyboardButton("✏️ Adjust", callback_data="food_adjust"),
            ],
            [InlineKeyboardButton("❌ Didn't eat it", callback_data="food_cancel")],
        ]
    )


def _hypothesis_summary(result: dict) -> str:
    """Compact test-setup message for a logged hypothesis — a summary and the tracking
    that's now live, not a prose read. Escapes user/LLM text for HTML parse mode."""

    def esc(s: str) -> str:
        return html.escape(s or "")

    lines = [f"🔬 <b>{esc(result.get('restatement'))}</b>"]
    if result.get("confirm_if"):
        lines.append(f"✅ Confirm: {esc(result['confirm_if'])}")
    if result.get("falsify_if"):
        lines.append(f"❌ Falsify: {esc(result['falsify_if'])}")

    if result.get("metrics"):
        lines.append("")
        for m in result["metrics"]:
            key = esc(m["key"])
            lines.append(
                f"📊 <b>{key}</b> — {esc(m.get('description', ''))} "
                f"(<code>metric: {key} &lt;value&gt;</code>)"
            )
    if result.get("habits"):
        lines.append("👁 Watch: " + esc(", ".join(result["habits"])))
    if result.get("follow_up_date"):
        fu = date.fromisoformat(result["follow_up_date"])
        lines.append(f"⏰ Check back {fu.strftime('%a %b %d')}")
    return "\n".join(lines)


def _format_food_estimate(raw: str, estimate: dict) -> str:
    """Telegram preview of the itemised estimate awaiting confirmation."""
    t = estimate["total"]
    item_lines = []
    for i in estimate["items"]:
        item_lines.append(
            f"• {html.escape(i['name'])} ({html.escape(str(i.get('portion', '')))}): "
            f"{round(i.get('kcal', 0))} kcal, {round(i.get('protein_g', 0))}g P"
        )
    breakdown = (
        f"<blockquote>{chr(10).join(item_lines)}</blockquote>" if item_lines else ""
    )
    total = (
        f"<b>Total:</b> ~{t['kcal']} kcal, {t['protein_g']}g protein, "
        f"{t['fat_g']}g fat, {t['carbs_g']}g carbs"
    )
    return (
        f"🍽 <b>Estimated:</b> {html.escape(raw)}\n\n"
        f"{breakdown}\n"
        f"{total}\n\n"
        f"<i>Estimates are approximate. Look right?</i>"
    )


def _food_log_content(raw: str, estimate: dict) -> str:
    """The entry stored in the log once confirmed: a summary line + per-item kcal."""
    t = estimate["total"]
    lines = [
        f"{raw} — ~{t['kcal']} kcal, {t['protein_g']}g protein, "
        f"{t['fat_g']}g fat, {t['carbs_g']}g carbs"
    ]
    for i in estimate["items"]:
        lines.append(
            f"  • {i['name']} ({i.get('portion', '')}): {round(i.get('kcal', 0))} kcal"
        )
    return "\n".join(lines)


class TextRouter:
    def __init__(self, bot, services, shabbat, allowed_user: int) -> None:
        self.bot = bot
        self.logs = services.logs
        self.agenda = services.agenda
        self.queue = services.queue
        self.backlog = services.backlog
        self.reminders = services.reminders
        self.gcal = services.gcal
        self.planner = services.planner
        self.hypotheses = services.hypotheses
        self.shabbat = shabbat
        self.allowed_user = allowed_user
        # Set by bot.py once both features exist — process_text commits user-added
        # agenda items through the agenda feature.
        self.agenda_feature = None
        # Set by bot.py — the grocery plugin, so confirmed voice transcripts opening
        # with "grocery"/"groceries" route into the list instead of a plain log.
        self.grocery = None
        # Set by bot.py after plugins are built — used to collect plugin classification
        # tags for the LLM and to dispatch LLM-classified messages to plugin handlers.
        self.plugins: list = []
        # Set by bot.py — the reclassify feature, which owns the Edit/Reclassify
        # buttons attached to every classified message and the low-confidence picker.
        self.reclassify = None
        # Conversation state owned here (single-user bot, in-memory is fine).
        self._awaiting_time: dict = {}  # chat_id -> partial reminder dict waiting for a time reply
        self._awaiting_candles: dict = {}  # chat_id -> True
        self._awaiting_voice_edit: dict = {}  # chat_id -> pending transcript text
        # chat_id -> prosodic features of the pending voice note (survives the
        # transcript-edit loop — the audio doesn't change when the text does).
        self._pending_affect: dict = {}
        # chat_id -> {"raw", "estimate", "adjusting": bool} for the food confirm flow
        self._awaiting_food: dict = {}

    def register(self, app: Application) -> None:
        app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))
        app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        app.add_handler(CommandHandler("backdate", self.cmd_backdate))
        app.add_handler(CommandHandler("undofood", self.cmd_undo_food))
        app.add_handler(
            CallbackQueryHandler(self.handle_voice_callback, pattern="^voice_")
        )
        app.add_handler(
            CallbackQueryHandler(self.handle_food_callback, pattern="^food_(?!del:)")
        )
        app.add_handler(
            CallbackQueryHandler(self.handle_food_delete_callback, pattern="^food_del:")
        )

    # --- Candle-lighting prompt state (shared with the morning_plan job) ---

    def expect_candle_time(self, chat_id: int) -> None:
        self._awaiting_candles[chat_id] = True

    # --- Pending-reply interceptors (called by bot.py's handle_message) ---

    async def try_handle_voice_edit(self, update: Update) -> bool:
        """If the user is editing a voice transcript, capture their reply and
        re-show the confirm/edit buttons. Returns True if it consumed the message."""
        chat_id = update.effective_chat.id
        if (
            chat_id in self._awaiting_voice_edit
            and self._awaiting_voice_edit[chat_id] == "__edit__"
        ):
            text = update.message.text.strip()
            self._awaiting_voice_edit[chat_id] = text
            keyboard = inline_keyboard_markup(
                [
                    [
                        InlineKeyboardButton("✅ OK", callback_data="voice_ok"),
                        InlineKeyboardButton("✏️ Edit", callback_data="voice_edit"),
                    ]
                ]
            )
            await update.message.reply_text(f'🎙 "{text}"', reply_markup=keyboard)
            return True
        return False

    async def try_handle_candle_reply(self, update: Update) -> bool:
        """If we asked for candle-lighting time, parse this reply. Returns True if
        it consumed the message."""
        chat_id = update.effective_chat.id
        if not self._awaiting_candles.pop(chat_id, False):
            return False
        text = update.message.text.strip()
        t = _parse_time(text)
        if t:
            self.shabbat.save_candle_lighting(t)
            await update.message.reply_text(self.shabbat.candle_confirmation(t))
            return True
        # Not a valid time. Only re-prompt if it actually looks like a time attempt;
        # otherwise the user has moved on (e.g. a check-in), so drop the candle prompt
        # and let this message be handled normally instead of hijacking it.
        if re.fullmatch(r"\s*\d{1,2}[:.\s]?\d{0,2}\s*", text):
            self._awaiting_candles[chat_id] = True
            await update.message.reply_text(
                "Couldn't parse that time. Send it again (e.g. 19:45)."
            )
            return True
        return False  # fall through — the candle await was already cleared by .pop()

    async def try_handle_time_reply(self, update: Update) -> bool:
        """If a reminder is waiting on a time, finish creating it. Returns True if
        it consumed the message."""
        chat_id = update.effective_chat.id
        if chat_id not in self._awaiting_time:
            return False
        partial = self._awaiting_time.pop(chat_id)
        text = update.message.text.strip()
        t = _parse_time(text)
        if not t:
            await update.message.reply_text(
                "Couldn't parse that as a time. Reminder cancelled."
            )
            return True
        from datetime import date as _date

        entry = self.reminders.add(
            text=partial["text"],
            reminder_type=partial["type"],
            **{k: v for k, v in partial.items() if k not in ("text", "type")},
            time=t,
        )
        d = entry.get("date", _date.today().isoformat())
        when = "today" if d == _date.today().isoformat() else d
        await update.message.reply_text(
            f'⏰ Reminder set: "{entry["text"]}" on {when} at {t}'
        )
        return True

    # --- Voice intake ---

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return

        tg_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        chat_id = update.effective_chat.id
        try:
            await tg_file.download_to_drive(tmp_path)
            # Synchronous Whisper round-trip (may be two passes for Arabic/Hebrew).
            # Off-load to a thread so the event loop stays free during the network call.
            result = await asyncio.to_thread(
                transcribe_with_language_detection, tmp_path
            )
            text = result["text"]
            # Local prosodic features from the same audio (librosa, no network).
            # Best-effort: a failed feature pass must never block the transcript.
            try:
                from affect import extract_affect

                self._pending_affect[chat_id] = await asyncio.to_thread(
                    extract_affect, tmp_path, len(text.split())
                )
            except Exception:
                logging.getLogger(__name__).exception("Affect extraction failed")
                self._pending_affect.pop(chat_id, None)
        finally:
            os.unlink(tmp_path)

        await send_sticker(self.bot, chat_id, "voice")
        self._awaiting_voice_edit[chat_id] = text
        keyboard = inline_keyboard_markup(
            [
                [
                    InlineKeyboardButton("✅ OK", callback_data="voice_ok"),
                    InlineKeyboardButton("✏️ Edit", callback_data="voice_edit"),
                ]
            ]
        )
        await update.message.reply_text(f'🎙 "{text}"', reply_markup=keyboard)

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ingest an uploaded HTML/text document: extract tasks + insights, add the
        tasks to today's agenda, log the insights, and report what was captured."""
        if update.effective_user.id != self.allowed_user:
            return
        doc = update.message.document
        if not doc:
            return
        name = doc.file_name or "document"
        lower = name.lower()
        is_text = lower.endswith((".html", ".htm", ".txt", ".md")) or (
            doc.mime_type or ""
        ).startswith("text")
        if not is_text:
            await update.message.reply_text(
                f"I can only read HTML/text files right now (got <code>{html.escape(name)}</code>).",
                parse_mode="HTML",
            )
            return
        if doc.file_size and doc.file_size > 2_000_000:
            await update.message.reply_text(
                "That file is over 2 MB — send a smaller export, please."
            )
            return

        await update.message.reply_text("📄 Reading the document…")
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
        markup = raw.decode("utf-8", errors="replace")
        text = (
            _html_to_text(markup) if lower.endswith((".html", ".htm")) else markup
        ).strip()
        if not text:
            await update.message.reply_text("Couldn't extract any text from that file.")
            return

        try:
            actions = await self.planner.extract_actions(text, source=name)
        except Exception as e:
            await update.message.reply_text(f"Couldn't process the document: {e}")
            return

        tasks, insights = actions.get("tasks", []), actions.get("insights", [])
        for ins in insights:
            self.logs.write("insight", ins)
        # Tasks go to the someday/backlog, not today's agenda — an uploaded doc is usually
        # planning material to review and pull from later, not today's to-dos.
        for t in tasks:
            self.backlog.add(t)

        if not tasks and not insights:
            await update.message.reply_text(
                f"📄 Read <b>{html.escape(name)}</b>, but found no clear action items.",
                parse_mode="HTML",
            )
            return

        lines = [f"📄 <b>From {html.escape(name)}:</b>"]
        if tasks:
            lines.append("\n<b>Added to your backlog</b> (review with /backlog)")
            lines += [f"• {html.escape(t)}" for t in tasks]
        if insights:
            lines.append("\n<b>Logged as insights</b>")
            lines += [f"• {html.escape(i)}" for i in insights]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A photo is treated as food: read the label / identify the dish via vision,
        then hand off to the same confirm/adjust UI the `food:` text path uses."""
        if update.effective_user.id != self.allowed_user:
            return
        photos = update.message.photo
        if not photos:
            return
        chat_id = update.effective_chat.id
        caption = (update.message.caption or "").strip()
        hint = re.sub(
            r"^(food|ate)\s*[:\s]\s*", "", caption, flags=re.IGNORECASE
        ).strip()

        await update.message.reply_text("📷 Looking at that…")
        tg_file = await photos[-1].get_file()  # [-1] = highest resolution
        img = bytes(await tg_file.download_as_bytearray())
        try:
            estimate = await self.planner.estimate_food_from_image(
                img, "image/jpeg", hint
            )
        except Exception as e:
            await update.message.reply_text(f"Couldn't read the image: {e}")
            return
        if not estimate:
            await update.message.reply_text(
                "Couldn't spot any food in that photo. If it is food, add a caption like "
                "<code>food: a bottle of kefir</code>.",
                parse_mode="HTML",
            )
            return

        raw = hint or "this"
        self._awaiting_food[chat_id] = {
            "raw": raw,
            "estimate": estimate,
            "adjusting": False,
        }
        await update.message.reply_text(
            _format_food_estimate(raw, estimate),
            reply_markup=_food_keyboard(),
            parse_mode="HTML",
        )

    async def handle_voice_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await safe_answer(query)
        chat_id = query.message.chat_id

        if query.data == "voice_ok":
            text = self._awaiting_voice_edit.pop(chat_id, None)
            if not text or text == "__edit__":
                await query.edit_message_text("⚠️ No pending transcript.")
                return
            await query.edit_message_text(f'🎙 "{text}"')

            def reply(msg, **kw):
                return context.bot.send_message(chat_id=chat_id, text=msg, **kw)

            affect = self._pending_affect.pop(chat_id, None)
            # "grocery …" voice notes go to the grocery list; anything else (or a
            # note that only looked like groceries) falls through to a normal log.
            if self.grocery and await self.grocery.handle_voice_text(text, reply):
                return
            await self.process_text(
                text,
                reply,
                chat_id=chat_id,
                extra={"affect_features": affect} if affect else None,
            )

        elif query.data == "voice_edit":
            current = self._awaiting_voice_edit.get(chat_id, "")
            self._awaiting_voice_edit[chat_id] = "__edit__"
            await query.edit_message_text(
                f"✏️ Copy, edit, and send back:\n\n<code>{html.escape(current)}</code>",
                parse_mode="HTML",
            )

    async def handle_food_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Confirm or adjust a pending food estimate."""
        query = update.callback_query
        await query.answer()
        if query.from_user.id != self.allowed_user:
            return
        chat_id = update.effective_chat.id
        pending = self._awaiting_food.get(chat_id)
        if not pending:
            await query.edit_message_text("⚠️ No pending food entry.")
            return

        if query.data == "food_confirm":
            self._awaiting_food.pop(chat_id, None)
            content = _food_log_content(pending["raw"], pending["estimate"])
            self.logs.write("food", content)
            await query.edit_message_text(
                _format_food_estimate(pending["raw"], pending["estimate"])
                + "\n\n✅ <b>Logged.</b>",
                parse_mode="HTML",
            )
        elif query.data == "food_adjust":
            pending["adjusting"] = True
            await query.edit_message_text(
                _format_food_estimate(pending["raw"], pending["estimate"])
                + "\n\n✏️ Tell me the correction (e.g. <i>the lasagna was ~400g, no salad</i>).",
                parse_mode="HTML",
            )
        elif query.data == "food_cancel":
            self._awaiting_food.pop(chat_id, None)
            await query.edit_message_text("👍 OK, not logged.")

    async def try_handle_food_adjust(self, update: Update) -> bool:
        """If a food estimate is awaiting a portion correction, re-estimate from the
        user's reply. Returns True if it consumed the message."""
        chat_id = update.effective_chat.id
        pending = self._awaiting_food.get(chat_id)
        if not pending or not pending.get("adjusting"):
            return False
        correction = update.message.text.strip()
        try:
            estimate = await self.planner.estimate_food(pending["raw"], correction)
        except Exception:
            estimate = None
        if not estimate:
            pending["adjusting"] = False
            await update.message.reply_text(
                "Couldn't re-estimate that. Log the original estimate, or cancel?",
                reply_markup=inline_keyboard_markup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Log original", callback_data="food_confirm"
                            ),
                            InlineKeyboardButton(
                                "❌ Don't log", callback_data="food_cancel"
                            ),
                        ]
                    ]
                ),
            )
            return True
        pending["estimate"] = estimate
        pending["adjusting"] = False
        await update.message.reply_text(
            _format_food_estimate(pending["raw"], estimate),
            reply_markup=_food_keyboard(),
            parse_mode="HTML",
        )
        return True

    # --- The dispatcher ---

    @staticmethod
    def _classify_entry(text: str) -> tuple[str, str]:
        """Map a raw message to (tag, content) using the prefix rules: a check-in, a
        known prefix (insight:, habit:, injection: …), or a bare #log. Shared by the live
        dispatcher and /backdate so both classify entries identically."""
        lower = _normalize(text.lower()).strip(".,!?;: ")
        checkin_m = _CHECKIN_RE.match(text)
        if checkin_m:
            return "checkin", (checkin_m.group(1) or "").strip()
        first_word_m = re.match(r"^(\w+)[,:.\s]\s*(.*)", lower, re.DOTALL)
        first_word = first_word_m.group(1) if first_word_m else ""
        for prefix, t in PREFIXES.items():
            keyword = prefix.rstrip(": ")
            if first_word == keyword or lower.startswith(prefix):
                content = re.sub(
                    r"^\w+[,:.\s]\s*", "", text, count=1, flags=re.IGNORECASE
                ).strip()
                return t.lstrip("#"), content
        # Rules-first: an entry carrying an explicit calorie + macro breakdown is
        # unambiguously food — tag it without an LLM call.
        if _is_nutrition_breakdown(text):
            return "food", text
        return "log", text

    def _food_manage_message(self) -> tuple[str, InlineKeyboardMarkup]:
        today = datetime.now(ZoneInfo("Asia/Jerusalem")).date()
        rows = self.logs.db.entries_for_date(today)
        food_rows = [r for r in rows if r["tag"] == "food"]
        if not food_rows:
            return "No food logged today.", inline_keyboard_markup([])
        kbd_rows = []
        for r in food_rows:
            label = r["content"][:40] + ("…" if len(r["content"]) > 40 else "")
            t = r["ts"][11:16]
            kbd_rows.append(
                [
                    InlineKeyboardButton(f"{t} {label}", callback_data="noop"),
                    InlineKeyboardButton("🗑", callback_data=f"food_del:{r['id']}"),
                ]
            )
        return "🍽 <b>Today's food — tap 🗑 to delete:</b>", inline_keyboard_markup(
            kbd_rows
        )

    async def cmd_undo_food(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/undofood — pick a food entry from today to delete."""
        if update.effective_user.id != self.allowed_user:
            return
        text, keyboard = self._food_manage_message()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def handle_food_delete_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        if query.from_user.id != self.allowed_user:
            return
        entry_id = int(query.data.split(":", 1)[1])
        self.logs.db.delete_entry(entry_id)
        text, keyboard = self._food_manage_message()
        if not inline_keyboard_rows(keyboard):
            await query.edit_message_text("🗑 Removed. No more food entries today.")
        else:
            await query.edit_message_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )

    async def _classify_entry_with_llm(
        self, text: str
    ) -> tuple[str, str, float | None]:
        """Like _classify_entry but falls back to Haiku when no prefix is detected.

        Gathers classification_tags from registered plugins so each plugin's tags
        are included in the LLM enum and prompt without hardcoding them here.

        Returns (tag, content, confidence). Confidence is only reported by the
        embedding classifier (its KNN vote share); prefix rules and the LLM path
        return None, which downstream treats as "no low-confidence prompt".
        """
        tag, content = self._classify_entry(text)
        if tag != "log":
            return tag, content, None
        # Rules-first: a bare entry that exactly matches a known habit string ("daily
        # walk") is routed to #habit deterministically — no LLM classification call.
        try:
            if habit := exact_habit_match(text, self.logs.db):
                return "habit", habit, None
        except Exception:
            pass
        # Only the genuinely ambiguous middle reaches a classifier. Which classifier is
        # swappable via OPS_CLASSIFIER ("llm" default, or "embedding" for the local KNN
        # prototype in classifier.py) so the two can run side-by-side without a rewrite.
        confidence = None
        try:
            extra_tags = [
                t
                for plugin in self.plugins
                for t in getattr(plugin, "classification_tags", [])
            ]
            if os.environ.get("OPS_CLASSIFIER") == "embedding":
                from classifier import classify_entry_embedding_confidence

                tag, confidence = await classify_entry_embedding_confidence(
                    text, self.logs.db, extra_tags=extra_tags or None
                )
            else:
                tag = await classify_entry(text, extra_tags=extra_tags or None)
        except Exception:
            pass  # keep "log" on any classifier failure
        return tag, content, confidence

    async def cmd_backdate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/backdate <date> <entry> — log an entry as of a past day (e.g. yesterday's
        Daf Yomi that never got logged). The remainder is parsed exactly like a normal
        message, but written with the resolved date so streaks and daily logs see it."""
        if update.effective_user.id != self.allowed_user:
            return
        reply = update.message.reply_text
        usage = (
            "Usage: <code>/backdate &lt;when&gt; &lt;entry&gt;</code>\n"
            "e.g. <code>/backdate yesterday habit: daf yomi</code>\n"
            "when can be: yesterday, today, a weekday (fri / last fri), "
            "<code>2 days ago</code>, <code>-1</code>, or an ISO date "
            "(<code>2026-06-06</code>)."
        )

        args = (update.message.text or "").split(maxsplit=1)
        if len(args) < 2 or not args[1].strip():
            await reply(usage, parse_mode="HTML")
            return

        when_date, entry_text = _parse_backdate(args[1])
        if when_date is None:
            await reply(
                "Couldn't read a past date there.\n\n" + usage, parse_mode="HTML"
            )
            return
        if not entry_text:
            await reply(
                "Got the date, but nothing to log.\n\n" + usage, parse_mode="HTML"
            )
            return

        tag, content, _ = await self._classify_entry_with_llm(entry_text)

        # Resolve free-text habit logs to a defined habit name, same as the live path, so
        # the backfilled day matches the checklist exactly and counts toward the streak.
        if tag == "habit":
            try:
                matched = await match_habit(content, self.logs.db)
                if matched:
                    content = matched
            except Exception:
                pass

        # Stamp the entry at the current time-of-day on the target date — habits/logs only
        # use the day, and a plausible time keeps within-day ordering sane.
        now = datetime.now(ZoneInfo("Asia/Jerusalem"))
        when = datetime.combine(when_date, now.timetz())
        try:
            self.logs.write(tag, content, when=when)
        except Exception as e:
            await reply(f"Couldn't save that: {e}")
            return

        await reply(
            f"⏪ Logged #{html.escape(tag)} for "
            f"<b>{when_date.strftime('%a %b %d')}</b>: {html.escape(content)}",
            parse_mode="HTML",
        )

    async def process_text(
        self, text: str, reply, chat_id: int = 0, extra: dict | None = None
    ) -> None:
        # `extra` rides on the logged entry's record (DB extra column + JSONL
        # line) — today that's a voice note's affect_features from handle_voice.
        text = _UNICODE_JUNK.sub("", text).strip()
        update_chat_id = chat_id
        lower = _normalize(text.lower()).strip(".,!?;: ")

        # edit N <text> — update agenda item text
        edit_match = re.match(r"^edit\s+(\d+)\s+(.+)$", lower)
        if edit_match:
            n = int(edit_match.group(1))
            open_items = self.agenda.get_open()
            if n < 1 or n > len(open_items):
                await reply(f"No open item #{n}.")
                return
            actual_id = open_items[n - 1]["id"]
            orig_match = re.match(
                r"^edit\s+\S+\s+(.*?)[\s.,!?;:]*$", text, re.IGNORECASE
            )
            new_text = orig_match.group(1) if orig_match else edit_match.group(2)
            old_text = self.agenda.edit_item(actual_id, new_text)
            self.logs.write("edit", f"item {n}: '{old_text}' → '{new_text}'")
            await reply(f"✏️ Item {n} updated.")
            return

        # done N / missed N — mark by number
        done_match = re.match(r"^(done|missed)\s+(\d+)$", lower)
        if done_match:
            action, n = done_match.group(1), int(done_match.group(2))
            open_items = self.agenda.get_open()
            if n < 1 or n > len(open_items):
                await reply(f"No open item #{n}.")
                return
            actual_id = open_items[n - 1]["id"]
            self.agenda.mark_status(actual_id, action)
            icon = "✅" if action == "done" else "❌"
            suffix = f" {encourage()}" if action == "done" else ""
            await reply(f"{icon} Item {n} marked {action}.{suffix}")
            return

        # done <name> / missed <name> — mark by fuzzy name match
        name_match = re.match(r"^(done|missed)\s+(.+)$", lower)
        if name_match:
            action, query_text = name_match.group(1), name_match.group(2)
            open_items = self.agenda.get_open()
            if open_items:
                item_texts = [i["text"].lower() for i in open_items]
                matches = difflib.get_close_matches(
                    query_text, item_texts, n=1, cutoff=0.3
                )
                if not matches:
                    # fallback: substring match
                    matches = [
                        t for t in item_texts if query_text in t or t in query_text
                    ]
                if matches:
                    item = open_items[item_texts.index(matches[0])]
                    self.agenda.mark_status(item["id"], action)
                    icon = "✅" if action == "done" else "❌"
                    suffix = f" {encourage()}" if action == "done" else ""
                    await reply(f'{icon} "{item["text"]}" marked {action}.{suffix}')
                    return
            await reply(f'Couldn\'t match "{query_text}" to any open agenda item.')
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
            event_text = text[lower.index(event_text) :].strip()
            await reply("📅 Parsing event…")
            try:
                parsed = await self.planner.parse_event(event_text)
                if not parsed:
                    await reply(
                        "Couldn't parse the event. Try: new calendar event: dentist tomorrow at 10am"
                    )
                    return
                tz = ZoneInfo("Asia/Jerusalem")
                start_dt = datetime.fromisoformat(
                    f"{parsed['date']}T{parsed['start_time']}:00"
                ).replace(tzinfo=tz)
                event = await asyncio.to_thread(
                    self.gcal.create_event,
                    parsed["summary"],
                    start_dt,
                    parsed.get("duration_minutes", 60),
                    parsed.get("description"),
                )
                link = event.get("htmlLink", "")
                await reply(
                    f"✅ Created: <b>{html.escape(parsed['summary'])}</b> on {parsed['date']} at {parsed['start_time']}\n{link}",
                )
            except Exception as e:
                await reply(f"Failed to create event: {e}")
            return

        # remind: / remind me — create a recurring reminder
        if lower.startswith("remind:") or lower.startswith("remind me"):
            reminder_text = re.sub(
                r"^remind(:|(\s+me\b))\s*", "", text, flags=re.IGNORECASE
            ).strip()
            await reply("⏰ Parsing reminder…")
            try:
                parsed = await self.planner.parse_reminder(reminder_text)
                if not parsed:
                    await reply(
                        "Couldn't parse the reminder. Try: remind: eat lunch at 13:00 or remind: drink water every 60 minutes"
                    )
                    return
                from datetime import date as _date

                extra = {k: v for k, v in parsed.items() if k not in ("text", "type")}
                if parsed["type"] == "once" and "date" not in extra:
                    extra["date"] = _date.today().isoformat()
                if parsed["type"] == "weekly" and "day_of_week" in parsed:
                    day_map = {
                        "monday": 0,
                        "tuesday": 1,
                        "wednesday": 2,
                        "thursday": 3,
                        "friday": 4,
                        "saturday": 5,
                        "sunday": 6,
                    }
                    extra["day"] = day_map.get(parsed["day_of_week"].lower(), 4)
                if (
                    parsed["type"] in ("once", "daily", "weekly")
                    and "time" not in extra
                ):
                    # ask for the time rather than defaulting
                    self._awaiting_time[update_chat_id] = {
                        "text": parsed["text"],
                        "type": parsed["type"],
                        **extra,
                    }
                    d = extra.get("date", _date.today().isoformat())
                    when = "today" if d == _date.today().isoformat() else d
                    await reply(f"What time on {when} should I remind you?")
                    return
                entry = self.reminders.add(
                    text=parsed["text"], reminder_type=parsed["type"], **extra
                )
                if entry["type"] == "once":
                    d = entry.get("date", _date.today().isoformat())
                    when = "today" if d == _date.today().isoformat() else d
                    await reply(
                        f'⏰ Reminder set: "{entry["text"]}" on {when} at {entry["time"]}'
                    )
                elif entry["type"] == "daily":
                    await reply(
                        f'⏰ Reminder set: "{entry["text"]}" every day at {entry["time"]}'
                    )
                elif entry["type"] == "weekly":
                    days = [
                        "Monday",
                        "Tuesday",
                        "Wednesday",
                        "Thursday",
                        "Friday",
                        "Saturday",
                        "Sunday",
                    ]
                    day_name = days[entry.get("day", 4)]
                    await reply(
                        f'⏰ Reminder set: "{entry["text"]}" every {day_name} at {entry["time"]}'
                    )
                else:
                    ws = entry.get("window_start", "08:00")
                    we = entry.get("window_end", "22:00")
                    await reply(
                        f'⏰ Reminder set: "{entry["text"]}" every {entry["interval_minutes"]} min ({ws}–{we})'
                    )
            except Exception as e:
                await reply(f"Failed to set reminder: {e}")
            return

        # backlog: / someday: — add to backlog
        if re.match(r"^(backlog|someday)[:\s]", lower):
            item_text = re.sub(
                r"^(backlog|someday)[:\s]\s*", "", text, flags=re.IGNORECASE
            ).strip()
            if item_text:
                self.backlog.add(item_text)
                await reply(f"📋 Added to backlog: {item_text}")
                return

        # shabbat / candle lighting — set quiet mode manually
        if re.match(r"^(shabbat mode|candle lighting|shabbos mode)", lower):
            # One-step: accept the time in the same message ("candle lighting 19:13").
            # Otherwise fall back to the prompt.
            rest = re.sub(
                r"^(shabbat mode|candle lighting|shabbos mode)[:\s]*",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            t = _parse_time(rest) if rest else None
            if t:
                self.shabbat.save_candle_lighting(t)
                await reply(self.shabbat.candle_confirmation(t))
            else:
                self._awaiting_candles[update_chat_id] = True
                await reply("🕯️ What time is candle lighting?")
            return

        # "add X to my agenda" / "X on the agenda" — an explicitly stated destination.
        # Must run before the queue matcher below (whose "add to" prefix would otherwise
        # swallow "add to my agenda") and before LLM classification (which would tag it
        # #task and drop the destination, so it never reached /agenda).
        if _AGENDA_DEST_RE.search(lower) and self.agenda_feature:
            item = _extract_agenda_item(text)
            if item:
                self.agenda_feature.commit_agenda([item], source="user")
                await reply(f"🗓 Added to agenda: {item}")
                return

        # queue for <day> [: | ,] <item> — add to a future agenda (works with voice)
        if re.match(r"^(?:queue|schedule|defer|add to)\b", lower):
            parsed = await parse_queue_entry(text)
            if parsed:
                target = _parse_queue_date(parsed["day"])
                if target:
                    self.queue.add(parsed["item"], target)
                    await reply(
                        f"📅 Queued for {target.strftime('%A %b %d')}: {parsed['item']}"
                    )
                    return
            await reply(
                "Couldn't parse that. Try: 'schedule for Sunday: deploy to VPS'"
            )

        # add: — user adds their own agenda item
        if lower.startswith("add:"):
            item_text = text[4:].strip()
            self.agenda_feature.commit_agenda([item_text], source="user")
            await reply(f"Added to agenda: {item_text}")
            return

        # sleep: 7 / slept 7 hours — log last night's sleep as the `sleep` metric. Only
        # fires when an explicit number is present, so "slept badly" stays a checkin.
        # (_normalize already turned "slept seven hours" → "slept 7 hours".)
        if re.match(r"^(?:sleep|slept)\b[:\s]", lower):
            hours_m = re.search(r"\d+(?:\.\d+)?", lower)
            if hours_m:
                hours = float(hours_m.group(0))
                self.logs.write_metric("sleep", hours, "h")
                await reply(f"😴 Sleep logged: {hours}h")
                return

        # metric(s): <key> <value> — structured metric entry. Accepts the plural
        # "metrics:", key/value in either order, and a filler word before the key
        # ("metrics: yesterday's steps 7095"). This was silently dropping plural /
        # possessive entries to #log, losing quantified readings.
        metric_prefix = re.match(
            r"^metrics?\b[,:.\s]+(.+)$", text, re.IGNORECASE | re.DOTALL
        )
        parsed_metric = (
            _parse_metric_body(metric_prefix.group(1)) if metric_prefix else None
        )
        if parsed_metric:
            key, value, unit, raw_val = parsed_metric
            try:
                self.logs.write_metric(key, value, unit)
            except Exception as e:
                # Don't fail silently: the reading is safe in JSONL (recoverable via
                # sync_jsonl_to_db), but tell the user it didn't reach the database.
                await reply(
                    f"⚠️ Metric NOT saved to DB: {key} = {raw_val}\n{e}\n(Kept in the log; run a sync to recover.)"
                )
                return
            await reply(f"📊 Metric logged: {key} = {raw_val}")
            return

        # feedback request — log it and respond with Claude's take
        feedback_m = _FEEDBACK_RE.match(text)
        if feedback_m:
            content = (feedback_m.group(1) or "").strip()
            if not content:
                await reply(
                    "What's on your mind? Send your idea or question after 'feedback:'"
                )
                return
            self.logs.write("feedback", content)
            await reply("💭 Thinking…")
            try:
                response_text = await self.planner.feedback(content)
                await reply(response_text)
            except Exception as e:
                await reply(f"Feedback failed: {e}")
            return

        # standard log entry — match prefix keyword regardless of trailing punctuation/case
        try:
            tag, content, confidence = await self._classify_entry_with_llm(text)
        except Exception:
            # LLM unavailable or any other failure — fall back to prefix-only so the
            # message is never lost. The user sees the correct (if less rich) tag.
            tag, content = self._classify_entry(text)
            confidence = None

        # Dispatch plugin-owned tags to the plugin that declared them. Each plugin
        # optionally implements handle_classified_text(tag, content, reply) → bool.
        for plugin in self.plugins:
            handler = getattr(plugin, "handle_classified_text", None)
            if handler is None:
                continue
            plugin_tags = [t["tag"] for t in getattr(plugin, "classification_tags", [])]
            if tag in plugin_tags:
                if await handler(tag, content, reply):
                    return

        # Food gets an itemised nutrition estimate the user confirms/adjusts before it's
        # logged. We hold the entry until they tap "Log it" rather than writing immediately.
        if tag == "food":
            try:
                estimate = await self.planner.estimate_food(content)
            except Exception:
                estimate = None
            if estimate:
                self._awaiting_food[chat_id] = {
                    "raw": content,
                    "estimate": estimate,
                    "adjusting": False,
                }
                await reply(
                    _format_food_estimate(content, estimate),
                    reply_markup=_food_keyboard(),
                    parse_mode="HTML",
                )
                return
            # No usable estimate — fall back to logging the raw description.
            entry_id = self.logs.write(tag, content, extra=extra)
            await reply(
                f"🍽 Logged: {content}",
                reply_markup=self._entry_keyboard(entry_id, tag, confidence, extra),
            )
            return

        # For free-text habit logs (e.g. "habit: took a stroll"), resolve which defined
        # habit it satisfies once, at log time, and store the canonical habit name — so the
        # checklist renders by exact match. The habit-specific resolution lives in the habit
        # module; the dispatcher just delegates.
        if tag == "habit":
            try:
                matched = await match_habit(content, self.logs.db)
                if matched:
                    content = matched
            except Exception:
                pass  # fall back to the raw text

        # Route through logs.write() so the entry lands in SQLite (primary) AND the JSONL
        # backup. Writing the file directly here bypassed the DB — the bug that made
        # prefix entries (values, insight, note, …) invisible to /values and other readers.
        entry_id = self.logs.write(tag, content, extra=extra)

        if tag in ("insight", "hypothesis"):
            await send_sticker(self.bot, chat_id, "idea")

        if tag == "discrete":
            await reply(
                "🔒 Logged.",
                reply_markup=self._entry_keyboard(entry_id, tag, confidence, extra),
            )
        elif tag == "checkin":
            # Mood/energy rows stay on top; the Edit/Reclassify row (or the
            # low-confidence picker) rides along underneath.
            mood_rows = inline_keyboard_rows(_mood_energy_keyboard())
            keyboard = self._entry_keyboard(entry_id, tag, confidence, extra)
            rc_rows = inline_keyboard_rows(keyboard)
            await reply(
                f"Logged #{tag} ✓",
                reply_markup=inline_keyboard_markup(mood_rows + rc_rows),
            )
        elif tag == "injection":
            await reply(
                f"💉 Injection logged: {html.escape(content)}",
                reply_markup=self._entry_keyboard(entry_id, tag, confidence, extra),
            )
        elif tag == "hypothesis":
            await reply(
                "Logged #hypothesis ✓ — setting up the test…",
                reply_markup=self._entry_keyboard(entry_id, tag, confidence, extra),
            )
            try:
                result = await self.planner.evaluate_hypothesis(content)
                metric_keys = [m["key"] for m in result.get("metrics", [])]
                self.hypotheses.add(
                    content,
                    restatement=result.get("restatement", ""),
                    confirm_if=result.get("confirm_if", ""),
                    falsify_if=result.get("falsify_if", ""),
                    metric_keys=metric_keys,
                    follow_up_date=result.get("follow_up_date", ""),
                )
                await reply(_hypothesis_summary(result), parse_mode="HTML")
            except Exception as e:
                await reply(f"Hypothesis logged but evaluation failed: {e}")
        else:
            if self._is_low_confidence(confidence):
                await reply(
                    f"Logged #{tag} ✓ — not sure about that tag. Right one?",
                    reply_markup=self._entry_keyboard(entry_id, tag, confidence, extra),
                )
            else:
                await reply(
                    f"Logged #{tag} ✓",
                    reply_markup=self._entry_keyboard(entry_id, tag, confidence, extra),
                )

    # --- Reclassify keyboard helpers ---

    def _is_low_confidence(self, confidence: float | None) -> bool:
        return (
            self.reclassify is not None
            and confidence is not None
            and confidence < self.reclassify.confidence_threshold
        )

    def _entry_keyboard(
        self,
        entry_id: int | None,
        tag: str,
        confidence: float | None,
        extra: dict | None = None,
    ) -> InlineKeyboardMarkup | None:
        """Per-entry action buttons for a just-logged message: the collapsed
        Edit/Reclassify pair normally, or the full category picker immediately
        (top guess pre-marked) when the classifier's confidence is low. Voice
        notes (extra carries affect_features) also get the optional 1-5
        self-mood-rating row — the ground truth for the local affect proxy."""
        if self.reclassify is None or entry_id is None:
            return None
        from reclassify_handlers import (
            entry_actions_keyboard,
            mood_rating_row,
            picker_keyboard,
        )

        extra_rows = (
            [mood_rating_row(entry_id)] if extra and extra.get("affect_features") else []
        )
        if self._is_low_confidence(confidence):
            return picker_keyboard(entry_id, tag, extra_rows=extra_rows)
        return entry_actions_keyboard(entry_id, extra_rows=extra_rows)

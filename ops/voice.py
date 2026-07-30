"""voice.py — read bot responses aloud: an on-demand 🔊 button on substantial
replies, plus a standing /voice on|off preference that also auto-sends audio.

Leaf cross-cutting module (same shape as media.py): plain functions, best-effort
(a synthesis failure is logged and swallowed — it must never take down the
underlying reply it's attached to). Provider selection is delegated entirely
to tts.py, so switching TTS providers never touches this module.
"""

import json
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton
from telegram.error import BadRequest

import tts
from tg_common import inline_keyboard_markup, inline_keyboard_rows

logger = logging.getLogger(__name__)

# Replies shorter than this are quick enough to just read; no button clutter.
_MIN_CHARS_FOR_BUTTON = 150

CALLBACK_DATA = "voice_speak"

_PREFS_PATH = (
    Path(os.environ.get("OPS_DATA_DIR", str(Path(__file__).parent / "log")))
    / "voice_prefs.json"
)


def is_auto_enabled() -> bool:
    """Whether the standing /voice on preference is set."""
    try:
        return bool(json.loads(_PREFS_PATH.read_text()).get("auto_enabled", False))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def set_auto_enabled(enabled: bool) -> None:
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_PATH.write_text(json.dumps({"auto_enabled": enabled}))


async def speak(
    bot, chat_id: int, text: str, reply_to_message_id: int | None = None
) -> None:
    """Synthesize `text` and send it as a playable audio message. Best-effort —
    failures are logged, never raised, so a bad TTS call can't break a reply."""
    if not text:
        return
    try:
        audio = await tts.get_provider().synthesize(text)
        await bot.send_audio(
            chat_id=chat_id,
            audio=audio,
            filename="speech.mp3",  # without this, PTB uploads it as
            # application/octet-stream and Telegram won't play it as audio
            title="personal_ops",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        logger.exception("Text-to-speech failed")


async def offer(bot, message) -> None:
    """Call after sending a substantial reply: attach a 🔊 Listen button, and if
    the standing auto-voice preference is on, also send the audio right away.

    `message` is the Message object `reply_text`/`send_message` returned — pass
    None to skip silently (e.g. when send_long swallowed an error upstream).
    """
    if message is None:
        return
    text = message.text or message.caption or ""
    if len(text) < _MIN_CHARS_FOR_BUTTON:
        return
    rows = inline_keyboard_rows(message.reply_markup)
    rows.append([InlineKeyboardButton("🔊 Listen", callback_data=CALLBACK_DATA)])
    try:
        await message.edit_reply_markup(inline_keyboard_markup(rows))
    except BadRequest:
        pass  # message already edited/deleted elsewhere — button is cosmetic
    if is_auto_enabled():
        await speak(bot, message.chat_id, text, reply_to_message_id=message.message_id)

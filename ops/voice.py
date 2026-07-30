"""voice.py — read bot responses aloud: an on-demand 🔊 button on substantial
replies, plus a standing /voice on|off preference that also auto-sends audio.

Leaf cross-cutting module (same shape as media.py): plain functions. A
synthesis failure never raises — it must never take down the underlying reply
it's attached to — but unlike media.py's stickers, this is a result the user
directly asked for by tapping a button, so a failure is reported back into the
chat instead of only going to the log; silently doing nothing is
indistinguishable from the feature being broken. Provider selection is
delegated entirely to tts.py, so switching TTS providers never touches this
module.
"""

import asyncio
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

# The openai SDK's own default timeout is long enough that a network-level
# hang (e.g. the host can't reach the provider at all) never raises — it just
# sits there forever, silent and indistinguishable from the feature being
# broken. Bound it ourselves so a hang always surfaces as a normal failure.
_SYNTHESIZE_TIMEOUT_S = 30

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
    """Synthesize `text` and send it as a playable audio message.

    Never raises — a bad TTS call must not break the reply it's attached to —
    but on failure it tells the chat why, rather than doing nothing visibly:
    the user triggered this directly (tap or /voice on), so silence here reads
    as "the feature doesn't work" instead of "here's the actual error."
    """
    if not text:
        return
    try:
        audio = await asyncio.wait_for(
            tts.get_provider().synthesize(text), timeout=_SYNTHESIZE_TIMEOUT_S
        )
        await bot.send_audio(
            chat_id=chat_id,
            audio=audio,
            filename="speech.mp3",  # without this, PTB uploads it as
            # application/octet-stream and Telegram won't play it as audio
            title="personal_ops",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as e:
        logger.exception("Text-to-speech failed")
        reason = (
            f"timed out after {_SYNTHESIZE_TIMEOUT_S}s (network issue reaching the "
            "TTS provider?)"
            if isinstance(e, asyncio.TimeoutError)
            else str(e) or type(e).__name__
        )
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🔊 Couldn't generate audio: {reason}",
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            logger.exception("Also failed to report the TTS error back to the chat")


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

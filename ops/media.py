"""Stickers + the startup video — a small delight layer.

Assets live in telegram_stickers/ at the repo root. Sending is always best-effort:
a missing file or a Telegram hiccup must never break the underlying action, so every
send is wrapped and failures are only logged.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent.parent / "telegram_stickers"

# Event kind → sticker file. Kinds are referenced by the handlers that fire them.
_STICKERS = {
    "done": "po_done.webm",
    "idea": "po_idea.webm",
    "missed": "po_missed.webm",
    "plan": "po_plan.webm",
    "reminder": "po_reminder.webm",
    "streak": "po_streak.webm",
    "voice": "po_voice.webm",
    "winddown": "po_winddown.webm",
}
_STARTUP_VIDEO = "back-up.mp4"


async def send_sticker(bot, chat_id: int, kind: str) -> None:
    """Send the sticker for an event kind. No-op if the file or kind is missing."""
    fname = _STICKERS.get(kind)
    if not fname:
        return
    path = _DIR / fname
    if not path.exists():
        return
    try:
        with open(path, "rb") as f:
            await bot.send_sticker(chat_id=chat_id, sticker=f)
    except Exception:
        logger.exception("Failed to send '%s' sticker", kind)


async def send_startup_video(bot, chat_id: int) -> None:
    """Send the 'back up' video once on startup. Best-effort."""
    path = _DIR / _STARTUP_VIDEO
    if not path.exists():
        return
    try:
        with open(path, "rb") as f:
            await bot.send_video(
                chat_id=chat_id, video=f, caption="🟢 personal_ops is back up."
            )
    except Exception:
        logger.exception("Failed to send startup video")

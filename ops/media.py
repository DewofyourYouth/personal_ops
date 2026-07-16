"""Stickers + the startup video — a small delight layer.

Assets live in telegram_stickers/ at the repo root. Sending is always best-effort:
a missing file or a Telegram hiccup must never break the underlying action, so every
send is wrapped and failures are only logged.

Stickers stay special by staying rare: call sites only fire them on notable moments
(streak milestones, clearing the agenda, the morning plan), and a per-kind cooldown
here is the backstop so no kind can become spam even if a call site fires often.
"""

import logging
import time
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
_STARTUP_ANIMATION = "restart.gif"

# Minimum seconds between sends of the same kind. 0 = never throttled (streak
# milestones are rare by construction). Kinds not listed get the default.
_DEFAULT_COOLDOWN_S = 6 * 3600
_COOLDOWN_S = {
    "streak": 0,
    "done": 2 * 3600,
}
_last_sent: dict[str, float] = {}


def _should_send(kind: str, now: float, last_sent: dict[str, float]) -> bool:
    """Pure cooldown check: has enough time passed since this kind last fired?"""
    cooldown = _COOLDOWN_S.get(kind, _DEFAULT_COOLDOWN_S)
    last = last_sent.get(kind)
    return last is None or now - last >= cooldown


async def send_sticker(bot, chat_id: int, kind: str) -> None:
    """Send the sticker for an event kind. No-op if the file or kind is missing,
    or if the kind is still inside its cooldown window."""
    fname = _STICKERS.get(kind)
    if not fname:
        return
    path = _DIR / fname
    if not path.exists():
        return
    now = time.monotonic()
    if not _should_send(kind, now, _last_sent):
        return
    _last_sent[kind] = now
    try:
        with open(path, "rb") as f:
            await bot.send_sticker(chat_id=chat_id, sticker=f)
    except Exception:
        logger.exception("Failed to send '%s' sticker", kind)


async def send_startup_animation(bot, chat_id: int) -> None:
    """Send the 'back up' GIF once on startup. Best-effort.

    GIFs render on Telegram only via sendAnimation (sendVideo rejects them with a
    BadRequest), so this uses send_animation.
    """
    path = _DIR / _STARTUP_ANIMATION
    if not path.exists():
        return
    try:
        with open(path, "rb") as f:
            await bot.send_animation(
                chat_id=chat_id, animation=f, caption="🟢 personal_ops is back up."
            )
    except Exception:
        logger.exception("Failed to send startup animation")

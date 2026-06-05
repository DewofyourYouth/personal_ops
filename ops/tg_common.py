"""Small Telegram UI utilities shared by the entrypoint and feature handlers.

Leaf module — imports nothing from bot.py, so feature classes can use these
without a circular import.
"""

import random

from telegram.error import BadRequest

from bot_constants import ENCOURAGEMENTS


async def safe_answer(query, text: str = "") -> None:
    try:
        await query.answer(text)
    except BadRequest:
        pass  # query expired (bot restarted, old button tapped)


def encourage() -> str:
    return random.choice(ENCOURAGEMENTS)

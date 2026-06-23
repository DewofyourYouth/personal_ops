"""Small Telegram UI utilities shared by the entrypoint and feature handlers.

Leaf module — imports nothing from bot.py, so feature classes can use these
without a circular import.
"""

import random

from telegram.error import BadRequest

from bot_constants import ENCOURAGEMENTS


TG_MAX_CHARS = 4000  # Telegram hard limit is 4096; leave a small buffer


async def send_long(reply, text: str, parse_mode: str = "HTML") -> None:
    """Send `text` as one or more messages, splitting at line boundaries so no
    single message exceeds the Telegram character limit. Preserves HTML integrity
    by only cutting at newlines (tags in this codebase never span lines)."""
    if len(text) <= TG_MAX_CHARS:
        await reply(text, parse_mode=parse_mode)
        return
    chunk_lines: list[str] = []
    chunk_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1  # +1 for the newline
        if chunk_lines and chunk_len + line_len > TG_MAX_CHARS:
            await reply("\n".join(chunk_lines), parse_mode=parse_mode)
            chunk_lines = []
            chunk_len = 0
        chunk_lines.append(line)
        chunk_len += line_len
    if chunk_lines:
        await reply("\n".join(chunk_lines), parse_mode=parse_mode)


async def safe_answer(query, text: str = "") -> None:
    try:
        await query.answer(text)
    except BadRequest:
        pass  # query expired (bot restarted, old button tapped)


def encourage() -> str:
    return random.choice(ENCOURAGEMENTS)

"""Small Telegram UI utilities shared by the entrypoint and feature handlers.

Leaf module — imports nothing from bot.py, so feature classes can use these
without a circular import.
"""

import html
import random

from telegram.error import BadRequest

from bot_constants import ENCOURAGEMENTS


TG_MAX_CHARS = 4000  # Telegram hard limit is 4096; leave a small buffer


def mono_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a column-aligned table wrapped in <pre> for Telegram HTML.

    Telegram's `parse_mode=HTML` does NOT support <table>/<tr>/<td> — sending them
    raises `BadRequest: unsupported start tag "table"` (real tables need the separate
    sendRichMessage/RichBlockTable API, which python-telegram-bot doesn't expose).
    A <pre> block is supported and renders monospaced, so space-padded columns line
    up. Cells are HTML-escaped; the surrounding <pre> is not double-escaped.
    """
    cols = [headers] + rows
    widths = [max(len(str(r[i])) for r in cols) for i in range(len(headers))]

    def fmt(cells: list[str]) -> str:
        padded = "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))
        return html.escape(padded.rstrip())

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines += [fmt(r) for r in rows]
    return "<pre>" + "\n".join(lines) + "</pre>"


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

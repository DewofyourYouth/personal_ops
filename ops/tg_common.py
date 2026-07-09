"""Small Telegram UI utilities shared by the entrypoint and feature handlers.

Leaf module — imports nothing from bot.py, so feature classes can use these
without a circular import.
"""

import html
import random

from telegram import InlineKeyboardMarkup
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


class _InlineKeyboardMarkupProxy:
    """Fallback for Telegram packages that serialize markups but don't expose rows."""

    def __init__(self, markup, rows: tuple[tuple, ...]) -> None:
        self._markup = markup
        self.inline_keyboard = rows

    def __getattr__(self, name):
        return getattr(self._markup, name)

    def to_dict(self) -> dict:
        if hasattr(self._markup, "to_dict"):
            return self._markup.to_dict()
        return {"inline_keyboard": self.inline_keyboard}


def inline_keyboard_markup(rows: list | tuple) -> InlineKeyboardMarkup:
    """Create an inline keyboard with a stable `.inline_keyboard` accessor.

    python-telegram-bot exposes `.inline_keyboard`, but some `telegram` package
    variants only serialize the markup. Tests and keyboard-composition code need
    row access, so attach the normalized rows when the object does not provide it.
    """
    normalized = tuple(tuple(row) for row in rows)
    markup = InlineKeyboardMarkup(normalized)
    if hasattr(markup, "inline_keyboard"):
        return markup
    try:
        markup.inline_keyboard = normalized
        return markup
    except Exception:
        return _InlineKeyboardMarkupProxy(markup, normalized)


def inline_keyboard_rows(markup) -> list[list]:
    if markup is None:
        return []
    rows = getattr(markup, "inline_keyboard", None)
    if rows is None:
        rows = getattr(markup, "keyboard", None)
    if rows is None:
        return []
    return [list(row) for row in rows]


def encourage() -> str:
    return random.choice(ENCOURAGEMENTS)

"""Grocery list plugin.

Owns a small SQLite-backed checklist, Telegram checkbox UI, and deterministic
natural-language capture for phrases like "pick up eggs and milk at the grocery".
"""

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from logs import Logs
from tg_common import safe_answer

TZ = ZoneInfo("Asia/Jerusalem")

# Voice notes opening with "grocery"/"groceries" route into the list. The rest of
# the transcript is the spoken list (or, if empty, a request to see the list).
_VOICE_PREFIX = re.compile(
    r"^\s*grocer(?:y|ies)\b[\s:,.\-–—]*(.*)$", re.IGNORECASE | re.DOTALL
)

_ITEMIZE_MODEL = "claude-haiku-4-5-20251001"

_GROCERY_DDL = """
CREATE TABLE IF NOT EXISTS grocery_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    checked    INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL DEFAULT 0,
    created_ts TEXT NOT NULL,
    checked_ts TEXT NOT NULL DEFAULT ''
);
"""


def _clean_item(item: str) -> str:
    item = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", item).strip()
    item = item.strip(" \t\r\n.,;:")
    item = re.sub(r"^(?:some|a|an|the)\s+", "", item, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", item).strip()


def split_grocery_items(text: str) -> list[str]:
    """Split a grocery item phrase into distinct item names."""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*(?:,?\s+and\s+|&|\+|;)\s*", ",", text, flags=re.IGNORECASE)
    items: list[str] = []
    seen: set[str] = set()
    for part in text.split(","):
        item = _clean_item(part)
        key = item.lower()
        if item and key not in seen:
            items.append(item)
            seen.add(key)
    return items


def parse_grocery_items(text: str) -> list[str]:
    """Extract grocery items from supported command-like natural-language phrases."""
    text = text.strip()
    patterns = [
        r"^(?:grocery\s+list|shopping\s+list|groceries|grocery)[:\s]+(.+)$",
        r"^(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:the\s+)?(?:grocery|groceries|grocery\s+list|shopping\s+list)$",
        r"^(?:pick\s+up|pickup|get|grab|buy)\s+(.+?)\s+(?:at|from)\s+(?:the\s+)?(?:grocery(?:\s+store)?|groceries|store|supermarket|market)$",
    ]
    for pattern in patterns:
        m = re.match(pattern, text, flags=re.IGNORECASE)
        if m:
            return split_grocery_items(m.group(1))
    return []


async def itemize_speech(text: str) -> list[str] | None:
    """Itemize a spoken grocery note via Claude.

    Returns a clean list of item names, or None if the note isn't actually a list
    of things to buy (e.g. "grocery store was packed today") — the caller then
    falls back to a regular log. Used for voice notes, where natural speech and
    filler words defeat the deterministic splitter.
    """
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model=_ITEMIZE_MODEL,
        max_tokens=256,
        tools=[
            {
                "name": "record_grocery_list",
                "description": (
                    "Record the grocery/shopping items mentioned in a spoken note. "
                    "Set is_grocery_list to false if the note is not actually a list "
                    "of things to buy (e.g. a passing comment about the grocery store)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "is_grocery_list": {
                            "type": "boolean",
                            "description": "True only if the note names items to buy.",
                        },
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Each grocery item as a short noun phrase, e.g. "
                                "'eggs', 'whole milk', 'bananas'. No filler words."
                            ),
                        },
                    },
                    "required": ["is_grocery_list", "items"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "record_grocery_list"},
        messages=[
            {"role": "user", "content": f"Itemize this spoken grocery note:\n\n{text}"}
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            data = block.input
            if not data.get("is_grocery_list"):
                return None
            items = [_clean_item(str(i)) for i in data.get("items", [])]
            return [i for i in items if i] or None
    return None


class GroceryStore:
    """SQLite-backed grocery checklist."""

    def __init__(self, db) -> None:
        self.db = db
        self.db.ensure_schema(_GROCERY_DDL)

    def add_items(self, items: list[str]) -> list[dict]:
        """Add new items, dedupe active ones, and reopen checked duplicates."""
        added_or_reopened: list[dict] = []
        existing = self.list(include_checked=True)
        by_key = {_clean_item(i["text"]).lower(): i for i in existing}
        now = datetime.now(TZ).isoformat(timespec="seconds")
        next_pos = self.db.query(
            "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM grocery_items"
        )[0]["p"]

        for raw in items:
            item = _clean_item(raw)
            if not item:
                continue
            key = item.lower()
            found = by_key.get(key)
            if found and not found["checked"]:
                continue
            if found and found["checked"]:
                self.db.execute(
                    "UPDATE grocery_items SET checked = 0, checked_ts = '', position = ? WHERE id = ?",
                    (next_pos, found["id"]),
                )
                reopened = {**found, "checked": False, "position": next_pos}
                added_or_reopened.append(reopened)
                by_key[key] = reopened
                next_pos += 1
                continue
            self.db.execute(
                "INSERT INTO grocery_items (text, checked, position, created_ts) VALUES (?, 0, ?, ?)",
                (item, next_pos, now),
            )
            row = self.db.query(
                "SELECT * FROM grocery_items WHERE id = last_insert_rowid()"
            )[0]
            added = self._row(row)
            added_or_reopened.append(added)
            by_key[key] = added
            next_pos += 1

        return added_or_reopened

    def list(self, include_checked: bool = True) -> list[dict]:
        where = "" if include_checked else "WHERE checked = 0"
        rows = self.db.query(
            f"SELECT * FROM grocery_items {where} ORDER BY checked, position, id"
        )
        return [self._row(r) for r in rows]

    def toggle(self, item_id: int) -> dict | None:
        rows = self.db.query("SELECT * FROM grocery_items WHERE id = ?", (item_id,))
        if not rows:
            return None
        item = self._row(rows[0])
        checked = 0 if item["checked"] else 1
        checked_ts = datetime.now(TZ).isoformat(timespec="seconds") if checked else ""
        self.db.execute(
            "UPDATE grocery_items SET checked = ?, checked_ts = ? WHERE id = ?",
            (checked, checked_ts, item_id),
        )
        rows = self.db.query("SELECT * FROM grocery_items WHERE id = ?", (item_id,))
        return self._row(rows[0])

    def clear_checked(self) -> int:
        count = self.db.query(
            "SELECT COUNT(*) AS n FROM grocery_items WHERE checked = 1"
        )[0]["n"]
        self.db.execute("DELETE FROM grocery_items WHERE checked = 1")
        return int(count)

    def copy_text(self) -> str:
        return "\n".join(i["text"] for i in self.list(include_checked=False))

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "text": row["text"],
            "checked": bool(row["checked"]),
            "position": row["position"],
        }


class GroceryHandlers:
    def __init__(self, bot: Bot, logs: Logs, allowed_user: int) -> None:
        self.bot = bot
        self.logs = logs
        self.allowed_user = allowed_user
        self.store = GroceryStore(logs.db)

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("grocery", self.cmd_grocery))
        app.add_handler(CommandHandler("groceries", self.cmd_grocery))
        app.add_handler(CommandHandler("addgrocery", self.cmd_add_grocery))
        app.add_handler(CommandHandler("grocerycopy", self.cmd_grocery_copy))
        app.add_handler(CommandHandler("cleargrocery", self.cmd_clear_grocery))
        app.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^gr_"))

    async def try_handle_text(self, update: Update, text: str) -> bool:
        items = parse_grocery_items(text)
        if not items:
            return False
        self.store.add_items(items)
        msg, keyboard = self._message(f"Added: {', '.join(items)}")
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
        return True

    async def handle_voice_text(self, text: str, reply) -> bool:
        """Voice-note entry point. If the transcript opens with "grocery"/"groceries",
        itemize the rest into the list (via the LLM, with a deterministic fallback).
        Returns True if it handled the note; False to let the caller fall back to a
        regular log — including when the note only looked like groceries.
        """
        m = _VOICE_PREFIX.match(text or "")
        if not m:
            return False
        body = m.group(1).strip()
        if not body:
            # Just "groceries" with nothing after it — show the current list.
            msg, keyboard = self._message()
            await reply(msg, parse_mode="HTML", reply_markup=keyboard)
            return True
        try:
            items = await itemize_speech(body)
        except Exception:
            items = split_grocery_items(body) or None
        if not items:
            return False  # not actually a grocery list → fall back to a regular log
        self.store.add_items(items)
        msg, keyboard = self._message(f"Added: {', '.join(items)}")
        await reply(msg, parse_mode="HTML", reply_markup=keyboard)
        return True

    async def cmd_grocery(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        notice = None
        if raw:
            items = split_grocery_items(raw)
            self.store.add_items(items)
            notice = f"Added: {', '.join(items)}" if items else None
        msg, keyboard = self._message(notice)
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)

    async def cmd_add_grocery(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        raw = " ".join(context.args).strip() if context.args else ""
        items = split_grocery_items(raw)
        if not items:
            await update.message.reply_text(
                "Usage: <code>/addgrocery eggs and milk</code>", parse_mode="HTML"
            )
            return
        self.store.add_items(items)
        msg, keyboard = self._message(f"Added: {', '.join(items)}")
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)

    async def cmd_grocery_copy(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if update.effective_user.id != self.allowed_user:
            return
        await update.message.reply_text(self._copy_message(), parse_mode="HTML")

    async def cmd_clear_grocery(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if update.effective_user.id != self.allowed_user:
            return
        count = self.store.clear_checked()
        msg, keyboard = self._message(f"Cleared {count} checked item(s).")
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)

    async def handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await safe_answer(query)
        if query.from_user.id != self.allowed_user:
            return

        data = query.data or ""
        if data.startswith("gr_t:"):
            try:
                self.store.toggle(int(data.split(":", 1)[1]))
            except ValueError:
                pass
            msg, keyboard = self._message()
            await query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
            return

        if data == "gr_clear":
            count = self.store.clear_checked()
            msg, keyboard = self._message(f"Cleared {count} checked item(s).")
            await query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
            return

        if data == "gr_copy":
            await query.message.reply_text(self._copy_message(), parse_mode="HTML")

    def _message(
        self, notice: str | None = None
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        items = self.store.list()
        if not items:
            lines = [
                "🛒 <b>Grocery list</b>",
                "",
                "No items yet.",
                "",
                "<code>/addgrocery eggs and milk</code>",
            ]
            if notice:
                lines.insert(2, html.escape(notice))
                lines.insert(3, "")
            return "\n".join(lines), None

        lines = ["🛒 <b>Grocery list</b>"]
        if notice:
            lines += ["", html.escape(notice)]
        lines.append("")
        for item in items:
            box = "☑" if item["checked"] else "☐"
            lines.append(f"{box} {html.escape(item['text'])}")

        copy_text = self.store.copy_text()
        if copy_text:
            lines += ["", "<b>Copy/share</b>", f"<pre>{html.escape(copy_text)}</pre>"]
        else:
            lines += ["", "<i>All checked off.</i>"]

        return "\n".join(lines), self._keyboard(items)

    def _keyboard(self, items: list[dict]) -> InlineKeyboardMarkup:
        rows = []
        for item in items:
            box = "☑" if item["checked"] else "☐"
            label = f"{box} {item['text']}"
            if len(label) > 42:
                label = label[:39].rstrip() + "..."
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"gr_t:{item['id']}")]
            )
        rows.append(
            [
                InlineKeyboardButton("Copy", callback_data="gr_copy"),
                InlineKeyboardButton("Clear checked", callback_data="gr_clear"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _copy_message(self) -> str:
        copy_text = self.store.copy_text()
        if not copy_text:
            return "No unchecked grocery items."
        return f"<pre>{html.escape(copy_text)}</pre>"

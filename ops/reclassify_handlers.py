"""Reclassification feature — low-friction label correction for classified entries.

A feature class (same shape as the other handlers): built with the bot and the
Logs service; commands and callbacks are methods that self-register via
`register(app)`.

Every classified message gets collapsed `✏️ Edit` / `🏷 Reclassify` buttons
(mirroring the voice transcript confirm/edit pattern). Tapping Reclassify swaps
the keyboard in place for a category picker; `/fix` re-opens the picker for the
most recent classified entry after it has scrolled away. When the embedding
classifier reports low confidence, the text router shows the picker immediately
with the top guess pre-marked, so confirming is also a single tap.

Every tap appends an append-only row to `label_events` — a correction
(`reclassify`) or a validated-correct label (`confirm`), both training data for
the weekly retrain loop. The original entry row keeps its history there
(`from_label`), but `entries.tag` IS updated to the corrected value: every
reader in the app (habit streaks, digests, the KNN reference set) keys off
`entries.tag`, and leaving it stale would defeat the correction. The JSONL
line of the original entry is never touched.

Callback data: `rc:<action>:<entry_id>[:<label>]`. Entry ids are SQLite integer
rowids, so even a 12-digit id with the longest tag stays ~35 bytes — comfortably
under Telegram's 64-byte callback_data cap; no short-token lookup table needed.
"""

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from llm import _BASE_CLASSIFICATION_TAGS
from tg_common import inline_keyboard_markup, inline_keyboard_rows, safe_answer

# Categories offered in the picker: the classifier enum plus the rules-routed
# tags a message realistically gets misfiled into or out of.
PICKER_TAGS = [tag for tag, _ in _BASE_CLASSIFICATION_TAGS] + ["food", "habit"]

# entries rows that never came from message classification — /fix skips them.
_NON_CLASSIFIED_TAGS = ("metric", "reminder", "edit", "agenda")


def entry_actions_keyboard(
    entry_id: int, extra_rows: list | None = None
) -> InlineKeyboardMarkup:
    """The collapsed per-entry buttons attached to every classified message."""
    row = [
        InlineKeyboardButton("✏️ Edit", callback_data=f"rc:edit:{entry_id}"),
        InlineKeyboardButton("🏷 Reclassify", callback_data=f"rc:menu:{entry_id}"),
    ]
    return inline_keyboard_markup([row] + list(extra_rows or []))


def picker_keyboard(
    entry_id: int, current_tag: str, extra_rows: list | None = None
) -> InlineKeyboardMarkup:
    """Category picker. The current/predicted tag is pre-marked with ✅ and taps
    as a *confirm* (validated-correct label), any other tag as a correction."""
    buttons = []
    for tag in PICKER_TAGS:
        if tag == current_tag:
            buttons.append(
                InlineKeyboardButton(
                    f"✅ {tag}", callback_data=f"rc:keep:{entry_id}:{tag}"
                )
            )
        else:
            buttons.append(
                InlineKeyboardButton(tag, callback_data=f"rc:set:{entry_id}:{tag}")
            )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"rc:cancel:{entry_id}")])
    return inline_keyboard_markup(rows + list(extra_rows or []))


# 1-5 self-rating for voice notes, matching the mood metric's 1-5 scale — the
# ground truth the local affect features get checked against later.
_MOOD_RATING_EMOJI = {1: "😞", 2: "😕", 3: "😐", 4: "😊", 5: "😄"}


def mood_rating_row(entry_id: int, locked: int | None = None) -> list:
    """The optional "how did you feel?" row on voice-note confirmations.
    Taps log a self_mood_rating metric; `locked` re-renders the chosen value."""
    return [
        InlineKeyboardButton(
            f"✅{n}" if locked == n else f"{_MOOD_RATING_EMOJI[n]}{n}",
            callback_data="noop" if locked is not None else f"sm:{entry_id}:{n}",
        )
        for n in sorted(_MOOD_RATING_EMOJI)
    ]


def _carried_rows(query) -> list:
    """Rows from the message's existing keyboard that aren't ours (e.g. the
    mood/energy or self-rating rows) — preserved across keyboard swaps."""
    keyboard = inline_keyboard_rows(query.message.reply_markup) if query.message else ()
    kept = []
    for row in keyboard or ():
        if not any((btn.callback_data or "").startswith("rc:") for btn in row):
            kept.append(list(row))
    return kept


class ReclassifyHandlers:
    def __init__(
        self, bot, logs, allowed_user: int, confidence_threshold: float = 0.55
    ) -> None:
        self.bot = bot
        self.logs = logs
        self.allowed_user = allowed_user
        self.confidence_threshold = confidence_threshold
        self._awaiting_edit: dict = {}  # chat_id -> entry_id whose content is being rewritten

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("fix", self.cmd_fix))
        app.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^rc:"))
        app.add_handler(CallbackQueryHandler(self.handle_mood_rating, pattern="^sm:"))

    # --- The correction/confirmation writes ---

    def apply_reclassify(self, entry_id: int, to_label: str) -> str | None:
        """Record the correction and update the live tag. Returns the old label,
        or None if the entry no longer exists."""
        entry = self.logs.db.entry_by_id(entry_id)
        if entry is None:
            return None
        from_label = entry["tag"]
        self.logs.log_label_event(entry_id, "reclassify", from_label, to_label)
        self.logs.db.update_entry_tag(entry_id, to_label)
        # Keep the recovery log consistent: sync_jsonl_to_db dedups by (ts, tag),
        # so the JSONL line must carry the corrected tag too (see rewrite_jsonl_entry).
        self.logs.rewrite_jsonl_entry(entry["ts"], entry["content"], new_tag=to_label)
        self._invalidate_classifier()
        return from_label

    def apply_confirm(self, entry_id: int, label: str) -> None:
        """A confirm is a validated-correct label — logged as its own event type,
        not a non-event; it's training data in its own right."""
        self.logs.log_label_event(entry_id, "confirm", label, label)

    @staticmethod
    def _invalidate_classifier() -> None:
        # The KNN reference set reads entries.tag, so a correction stales the
        # cached singleton. Import lazily: classifier pulls in numpy/openai.
        try:
            from classifier import reset_singleton

            reset_singleton()
        except Exception:
            pass

    # --- /fix ---

    async def cmd_fix(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/fix — reopen the category picker for the most recent classified entry
        (covers the case where the original message has scrolled away)."""
        if update.effective_user.id != self.allowed_user:
            return
        entry = self.logs.db.latest_entry(exclude_tags=_NON_CLASSIFIED_TAGS)
        if entry is None:
            await update.message.reply_text("Nothing classified yet — nothing to fix.")
            return
        preview = entry["content"][:120] + ("…" if len(entry["content"]) > 120 else "")
        await update.message.reply_text(
            f"🏷 <b>#{html.escape(entry['tag'])}</b> — “{html.escape(preview)}”\n"
            "Pick the right category:",
            parse_mode="HTML",
            reply_markup=picker_keyboard(entry["id"], entry["tag"]),
        )

    # --- Callbacks ---

    async def handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await safe_answer(query)
        if query.from_user.id != self.allowed_user:
            return
        parts = query.data.split(":")  # rc:<action>:<entry_id>[:<label>]
        action, entry_id = parts[1], int(parts[2])

        match action:
            case "menu":
                entry = self.logs.db.entry_by_id(entry_id)
                if entry is None:
                    await query.edit_message_reply_markup(reply_markup=None)
                    return
                await query.edit_message_reply_markup(
                    reply_markup=picker_keyboard(
                        entry_id, entry["tag"], extra_rows=_carried_rows(query)
                    )
                )

            case "cancel":
                await query.edit_message_reply_markup(
                    reply_markup=entry_actions_keyboard(
                        entry_id, extra_rows=_carried_rows(query)
                    )
                )

            case "set":
                to_label = parts[3]
                from_label = self.apply_reclassify(entry_id, to_label)
                if from_label is None:
                    await query.edit_message_text("⚠️ That entry no longer exists.")
                    return
                if from_label == to_label:
                    # Same tag tapped from an unmarked button (stale keyboard) —
                    # it's a confirmation, not a correction.
                    self.apply_confirm(entry_id, to_label)
                    await self._show_confirmed(query, to_label)
                    return
                # Edit the original message in place so the visible chat history
                # matches the corrected state: old label struck, new one checked.
                await query.edit_message_text(
                    f"Logged <s>#{html.escape(from_label)}</s> → "
                    f"<b>#{html.escape(to_label)}</b> ✅",
                    parse_mode="HTML",
                )

            case "keep":
                self.apply_confirm(entry_id, parts[3])
                await self._show_confirmed(query, parts[3])

            case "edit":
                entry = self.logs.db.entry_by_id(entry_id)
                if entry is None:
                    await query.edit_message_text("⚠️ That entry no longer exists.")
                    return
                self._awaiting_edit[query.message.chat_id] = entry_id
                await query.edit_message_text(
                    "✏️ Copy, edit, and send back:\n\n"
                    f"<code>{html.escape(entry['content'])}</code>",
                    parse_mode="HTML",
                )

    async def handle_mood_rating(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """A self_mood_rating tap on a voice-note message: log the metric and
        lock the row in place (other rows on the message stay live)."""
        query = update.callback_query
        await safe_answer(query)
        if query.from_user.id != self.allowed_user:
            return
        _, entry_id, rating = query.data.split(":")  # sm:<entry_id>:<n>
        self.logs.write_metric("self_mood_rating", int(rating))
        rows = []
        for row in inline_keyboard_rows(query.message.reply_markup):
            if any((btn.callback_data or "").startswith("sm:") for btn in row):
                rows.append(mood_rating_row(int(entry_id), locked=int(rating)))
            else:
                rows.append(list(row))
        try:
            await query.edit_message_reply_markup(
                reply_markup=inline_keyboard_markup(rows)
            )
        except Exception:
            pass  # unchanged markup / expired query — the metric is saved either way

    @staticmethod
    async def _show_confirmed(query, label: str) -> None:
        await query.edit_message_text(
            f"Logged <b>#{html.escape(label)}</b> ✅ (confirmed)", parse_mode="HTML"
        )

    # --- Pending-reply interceptor (called by bot.py's handle_message) ---

    async def try_handle_edit_reply(self, update: Update) -> bool:
        """If an entry's content is being rewritten, capture the reply and update
        the stored entry. Returns True if it consumed the message."""
        chat_id = update.effective_chat.id
        if chat_id not in self._awaiting_edit:
            return False
        entry_id = self._awaiting_edit.pop(chat_id)
        new_content = update.message.text.strip()
        entry = self.logs.db.entry_by_id(entry_id)
        if entry is None or not new_content:
            await update.message.reply_text("⚠️ Couldn't update that entry.")
            return True
        self.logs.db.update_entry_content(entry_id, new_content)
        self.logs.rewrite_jsonl_entry(
            entry["ts"], entry["content"], new_content=new_content
        )
        await update.message.reply_text(
            f"✏️ Updated <b>#{html.escape(entry['tag'])}</b>: {html.escape(new_content)}",
            parse_mode="HTML",
            reply_markup=entry_actions_keyboard(entry_id),
        )
        return True

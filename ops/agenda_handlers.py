"""Agenda feature — Telegram handlers for the daily proposal + agenda flow.

A feature class: constructed with the bot and the domain services it needs;
its handlers are methods; the proposal conversation state (`_pending`) is
instance state rather than a module global. Imports only leaf modules, so it
never depends on bot.py.

Note: `morning_plan` (the scheduled job) stays in bot.py — it mixes agenda with
the Friday candle-lighting prompt — and calls `send_proposal` here.
"""

import asyncio
import html

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from agenda import Agenda
from agenda_queue import AgendaQueue
from bot_constants import STATUS_ICONS
from gcal import GCal
from logs import Logs
from planner import Planner, day_type
from tg_common import encourage, safe_answer


class AgendaHandlers:
    def __init__(
        self,
        bot: Bot,
        agenda: Agenda,
        queue: AgendaQueue,
        gcal: GCal,
        planner: Planner,
        logs: Logs,
        allowed_user: int,
    ) -> None:
        self.bot = bot
        self.agenda = agenda
        self.queue = queue
        self.gcal = gcal
        self.planner = planner
        self.logs = logs
        self.allowed_user = allowed_user
        self._pending: dict = {}  # chat_id -> {"items", "selected", ("editing")}

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("plan", self.cmd_plan))
        app.add_handler(CommandHandler("p", self.cmd_plan))
        app.add_handler(CommandHandler("agenda", self.cmd_agenda))
        app.add_handler(CommandHandler("a", self.cmd_agenda))
        app.add_handler(CommandHandler("status", self.cmd_agenda_status))
        app.add_handler(
            CallbackQueryHandler(self.handle_agenda_callback, pattern="^ag_")
        )
        app.add_handler(
            CallbackQueryHandler(self.handle_proposal_callback, pattern="^pt_")
        )

    # --- Proposal UI helpers ---

    @staticmethod
    def _proposal_keyboard(
        items: list[str], selected: set[int]
    ) -> InlineKeyboardMarkup:
        rows = []
        for i, item in enumerate(items):
            mark = "✅" if i in selected else "⬜"
            label = item if len(item) <= 32 else item[:29] + "…"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark} {i + 1}. {label}", callback_data=f"pt_t:{i}"
                    ),
                    InlineKeyboardButton("✏️", callback_data=f"pt_e:{i}"),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton("Confirm", callback_data="pt_ok"),
                InlineKeyboardButton("Accept All", callback_data="pt_all"),
                InlineKeyboardButton("Cancel", callback_data="pt_no"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _proposal_text(items: list[str], selected: set[int]) -> str:
        lines = [f"📋 <b>Proposed agenda ({html.escape(day_type())}):</b>\n"]
        for i, item in enumerate(items):
            mark = "✅" if i in selected else "⬜"
            lines.append(f"{mark} {i + 1}. {html.escape(item)}")
        lines.append("\n<i>Tap items to toggle, then Confirm.</i>")
        return "\n".join(lines)

    @staticmethod
    def _agenda_message(open_items: list) -> tuple[str, InlineKeyboardMarkup]:
        lines = ["📋 <b>Open items:</b>\n"]
        rows = []
        for i, item in enumerate(open_items, 1):
            lines.append(f"{i}. {html.escape(item['text'])}")
            rows.append(
                [
                    InlineKeyboardButton(
                        f"✅ {i} Done", callback_data=f"ag_done:{item['id']}"
                    ),
                    InlineKeyboardButton(
                        f"❌ {i} Missed", callback_data=f"ag_missed:{item['id']}"
                    ),
                ]
            )
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    @staticmethod
    def _status_message(items: list) -> str:
        lines = ["Agenda Status:\n"]
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. {html.escape(STATUS_ICONS[item['status']])} {html.escape(item['text'])}"
            )
        return "\n".join(lines)

    # --- Commands ---

    async def cmd_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        await update.message.reply_text("Generating today's agenda…")
        await self.send_proposal(update.effective_chat.id)

    async def cmd_agenda(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        open_items = self.agenda.get_open()
        if not open_items:
            await update.message.reply_text(
                "No open agenda items. Use /plan to generate one."
            )
            return
        text, keyboard = self._agenda_message(open_items)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def cmd_agenda_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if update.effective_user.id != self.allowed_user:
            return
        items = self.agenda.get_status()
        if not items:
            await update.message.reply_text(
                "No open agenda items. Use /plan to generate one."
            )
            return
        await update.message.reply_text(self._status_message(items), parse_mode="HTML")

    # --- Callbacks ---

    async def handle_agenda_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        action, item_id = query.data.split(":")[0], int(query.data.split(":")[1])
        status = "done" if action == "ag_done" else "missed"
        self.agenda.mark_status(item_id, status)

        await safe_answer(query, encourage() if status == "done" else "Marked missed.")

        open_items = self.agenda.get_open()
        if not open_items:
            await query.edit_message_text("✅ All items resolved.")
            return
        text, keyboard = self._agenda_message(open_items)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def handle_proposal_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await safe_answer(query)

        chat_id = update.effective_chat.id
        if chat_id not in self._pending:
            await query.edit_message_text(
                "No pending proposal — use /plan to generate one."
            )
            return

        state = self._pending[chat_id]
        items, selected = state["items"], state["selected"]
        data = query.data

        if data.startswith("pt_t:"):
            idx = int(data.split(":")[1])
            selected.symmetric_difference_update({idx})
            await query.edit_message_text(
                self._proposal_text(items, selected),
                parse_mode="HTML",
                reply_markup=self._proposal_keyboard(items, selected),
            )

        elif data == "pt_all":
            accepted = list(items)
            self.commit_proposal(accepted, [])
            del self._pending[chat_id]
            await query.edit_message_text(
                f"✅ Accepted all {len(accepted)} items. Agenda set."
            )

        elif data == "pt_ok":
            accepted = [items[i] for i in sorted(selected)]
            rejected = [items[i] for i in range(len(items)) if i not in selected]
            self.commit_proposal(accepted, rejected)
            del self._pending[chat_id]
            if accepted:
                lines = "\n".join(f"• {html.escape(t)}" for t in accepted)
                await query.edit_message_text(
                    f"✅ Agenda set ({len(accepted)} items):\n{lines}",
                    parse_mode="HTML",
                )
            else:
                # No minimum: rejecting everything is a valid, respected outcome.
                await query.edit_message_text("👍 No agenda items today — all set.")

        elif data.startswith("pt_e:"):
            idx = int(data.split(":")[1])
            state["editing"] = idx
            await safe_answer(query, f"Send new text for item {idx + 1}:")
            await query.message.reply_text(f"✏️ Send new text for item {idx + 1}:")

        elif data == "pt_no":
            del self._pending[chat_id]
            await query.edit_message_text("Proposal discarded.")

    async def try_handle_proposal_edit(self, update: Update) -> bool:
        """If the user is mid-edit of a proposed item, apply their reply and
        re-render. Called by the central message dispatcher; returns True if it
        consumed the message."""
        chat_id = update.effective_chat.id
        if chat_id in self._pending and "editing" in self._pending[chat_id]:
            text = update.message.text.strip()
            idx = self._pending[chat_id].pop("editing")
            self._pending[chat_id]["items"][idx] = text
            state = self._pending[chat_id]
            await update.message.reply_text(
                self._proposal_text(state["items"], state["selected"]),
                parse_mode="HTML",
                reply_markup=self._proposal_keyboard(state["items"], state["selected"]),
            )
            return True
        return False

    # --- Commit ---

    def commit_agenda(self, texts: list[str], source: str = "llm") -> None:
        items = self.agenda.accept_items(texts, source=source)
        self.agenda.write_to_markdown(items)

    def commit_proposal(
        self, accepted: list[str], rejected: list[str], source: str = "llm"
    ) -> None:
        """Commit a proposal decision: add accepted items and remove rejected ones
        (so unchecking truly rejects). Rejections are logged as signal — a temporary
        `agenda_reject` tag until the interventions table lands."""
        new_items = self.agenda.reconcile(accepted, rejected, source=source)
        for text in rejected:
            self.logs.write("agenda_reject", text)
        if new_items:
            self.agenda.write_to_markdown(new_items)

    # --- Proposal generation ---

    async def send_proposal(self, chat_id: int) -> None:
        calendar_events = ""
        try:
            events = await asyncio.to_thread(self.gcal.get_today_events)
            calendar_events = self.gcal.format_events(events)
        except Exception:
            pass  # calendar is optional — plan without it if unavailable

        # inject any items queued for today
        queued = self.queue.pop_for_today()
        if queued:
            self.agenda.accept_items(queued, source="queued")

        try:
            items = await self.agenda.generate(self.planner, calendar_events)
        except Exception as e:
            await self.bot.send_message(
                chat_id=chat_id, text=f"Agenda generation failed: {e}"
            )
            return

        if not items:
            await self.bot.send_message(
                chat_id=chat_id, text="No agenda items returned — try again."
            )
            return

        # Collapse semantic duplicates the exact-match guard can't catch: items that
        # restate each other, or restate something already open (e.g. a queued item the
        # LLM re-proposed in different words). Existing open items are passed as context
        # so their duplicates drop out of the proposal entirely.
        existing_open = [it["text"] for it in self.agenda.get_open()]
        items = await self.planner.dedupe(existing_open, items)
        if not items:
            await self.bot.send_message(
                chat_id=chat_id,
                text="Nothing new to propose — today's agenda already covers it.",
            )
            return

        selected = set(range(len(items)))
        self._pending[chat_id] = {"items": items, "selected": selected}

        await self.bot.send_message(
            chat_id=chat_id,
            text=self._proposal_text(items, selected),
            parse_mode="HTML",
            reply_markup=self._proposal_keyboard(items, selected),
        )

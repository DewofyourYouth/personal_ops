"""Status feature — the /status snapshot the user pulls during the day.

A cross-cutting dashboard: it doesn't own a domain, it composes four that do —
open habits (habit feature), open agenda (agenda feature), what's left on today's
calendar (gcal), and a short LLM read on how the day is going (planner). Each piece
is owned by its own feature/service; this just assembles them.

The snapshot is sent as a rich message (Bot API 10.1) so the habits render as a
native checkbox list — done habits checked, the rest unchecked. PTB 22.7 has no
binding for it, so we go through the tiny raw-API helper in rich.py and fall back
to a plain HTML message if that send fails, since the endpoint is only days old.

The synopsis is the only LLM call and it can be slow, so the deterministic
snapshot is sent first and the synopsis follows when it's ready.

Like the router, it's handed its sibling features after they're built (agenda +
habits), since those are constructed elsewhere in the composition root.
"""

import asyncio
import html
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from agenda_handlers import AgendaHandlers
from bot_constants import STATUS_ICONS
from gcal import GCal
from habit_handlers import HabitHandlers
from planner import Planner, day_type
from rich import send_rich_message

_TZ = ZoneInfo("Asia/Jerusalem")
_log = logging.getLogger(__name__)


class StatusHandlers:
    def __init__(
        self,
        bot: Bot,
        agenda_feature: AgendaHandlers,
        gcal: GCal,
        planner: Planner,
        shabbat,
        allowed_user: int,
    ) -> None:
        self.bot = bot
        self.agenda_feature = agenda_feature
        self.gcal = gcal
        self.planner = planner
        self.shabbat = shabbat
        self.allowed_user = allowed_user
        # Set in the composition root once the habit plugin is built.
        self.habits: HabitHandlers | None = None

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("s", self.cmd_status))

    # --- Section rendering (no Telegram, no LLM — easy to unit test) ---

    def _habits_section(self) -> str:
        if self.shabbat.quiet_now():
            return "🔥 <b>Habits</b>\n🕯 Shabbat — habits aren't tracked now."
        if self.habits is None:
            return ""
        pending = self.habits.pending_today()
        if not pending:
            return "🔥 <b>Open Habits</b>\n✅ All habits accounted for today."
        lines = [f"🔥 <b>Open Habits ({len(pending)})</b>"]
        lines += [f"⬜ {html.escape(name)}" for name in pending]
        return "\n".join(lines)

    def _agenda_section(self) -> str:
        status = self.agenda_feature.status_text()
        if status is None:
            return "📋 <b>Agenda</b>\nNo agenda yet — /plan to generate one."
        return "📋 <b>Agenda</b>\n" + html.escape(status)

    def _events_section(self, events_text: str) -> str:
        return f"📅 <b>Upcoming today</b>\n{html.escape(events_text)}"

    def _snapshot_message(self, events_text: str) -> str:
        now = datetime.now(_TZ)
        header = f"📊 <b>Status</b> — {now.strftime('%A %b %d, %H:%M')} ({html.escape(day_type())})"
        sections = [
            header,
            self._habits_section(),
            self._agenda_section(),
            self._events_section(events_text),
        ]
        return "\n\n".join(s for s in sections if s)

    # --- Rich-message rendering (Bot API 10.1 block HTML; also pure/testable) ---

    def _rich_habits_html(self) -> str | None:
        """Habits as a native checkbox <ul>: done habits checked, the rest open.
        None when there's no habit plugin wired (section simply omitted)."""
        if self.shabbat.quiet_now():
            return "<p><b>🔥 Habits</b><br>🕯 Shabbat — habits aren't tracked now.</p>"
        if self.habits is None:
            return None
        checklist = self.habits.today_checklist()
        if not checklist:
            return "<p><b>🔥 Habits</b><br>No habits due today.</p>"
        open_n = sum(1 for _, done in checklist if not done)
        title = (
            "✅ All habits done today" if open_n == 0 else f"🔥 Open Habits ({open_n})"
        )
        items = "".join(
            f'<li><input type="checkbox"{" checked" if done else ""}>'
            f"{html.escape(name)}</li>"
            for name, done in checklist
        )
        return f"<p><b>{title}</b></p><ul>{items}</ul>"

    def _rich_agenda_html(self) -> str:
        """Agenda as a table: number, status icon, item. The number column keeps the
        'done 2' / 'missed 3' marking workflow legible."""
        items = self.agenda_feature.status_items()
        if not items:
            return "<p><b>📋 Agenda</b><br>No agenda yet — /plan to generate one.</p>"
        head = '<tr><th>#</th><th></th><th align="left">Item</th></tr>'
        rows = "".join(
            f"<tr><td>{i}</td>"
            f"<td>{html.escape(STATUS_ICONS[it['status']])}</td>"
            f'<td align="left">{html.escape(it["text"])}</td></tr>'
            for i, it in enumerate(items, 1)
        )
        return f"<p><b>📋 Agenda</b></p><table>{head}{rows}</table>"

    def _rich_events_html(self, rows: list[tuple[str, str]], note: str) -> str:
        """Today's remaining events as a Time | Event table. `note` is shown instead
        when there are no rows (e.g. 'No events today.' or 'Calendar unavailable.')."""
        if not rows:
            return f"<p><b>📅 Upcoming today</b><br>{html.escape(note)}</p>"
        head = '<tr><th align="left">Time</th><th align="left">Event</th></tr>'
        body = "".join(
            f'<tr><td>{html.escape(t)}</td><td align="left">{html.escape(s)}</td></tr>'
            for t, s in rows
        )
        return f"<p><b>📅 Upcoming today</b></p><table>{head}{body}</table>"

    def _rich_snapshot_html(self, event_rows: list[tuple[str, str]], note: str) -> str:
        now = datetime.now(_TZ)
        header = (
            f"<p><b>📊 Status</b> — {html.escape(now.strftime('%A %b %d, %H:%M'))} "
            f"({html.escape(day_type())})</p>"
        )
        parts = [
            header,
            self._rich_habits_html(),
            self._rich_agenda_html(),
            self._rich_events_html(event_rows, note),
        ]
        return "".join(p for p in parts if p)

    # --- Command ---

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return

        # Calendar is optional — a snapshot without it is still useful. We keep both
        # the structured rows (for the rich table) and the text form (for the fallback).
        try:
            events = await asyncio.to_thread(self.gcal.get_today_events)
            event_rows = self.gcal.event_rows(events)
            events_text = self.gcal.format_events(events)
        except Exception:
            event_rows = []
            events_text = "Calendar unavailable."

        # Send the snapshot as a rich message so habits render as a checkbox list and
        # agenda/events as tables. The endpoint is days old and unsupported by PTB, so
        # any failure falls back to the plain HTML snapshot — /status must never break.
        try:
            await send_rich_message(
                self.bot.token,
                update.effective_chat.id,
                self._rich_snapshot_html(event_rows, events_text),
            )
        except Exception:
            _log.warning("sendRichMessage failed; falling back to HTML", exc_info=True)
            await update.message.reply_text(
                self._snapshot_message(events_text), parse_mode="HTML"
            )

        # The synopsis is the slow part (an LLM call): send it as a follow-up so the
        # deterministic snapshot lands immediately. A failure here never blocks it.
        try:
            synopsis = await self.planner.day_synopsis(target_date=date.today())
            if synopsis:
                await update.message.reply_text(
                    f"📝 <b>How it's going</b>\n{html.escape(synopsis)}",
                    parse_mode="HTML",
                )
        except Exception:
            pass

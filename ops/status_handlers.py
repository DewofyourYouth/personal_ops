"""Status feature — the /status snapshot the user pulls during the day.

A cross-cutting dashboard: it doesn't own a domain, it composes four that do —
open habits (habit feature), open agenda (agenda feature), what's left on today's
calendar (gcal), and a short LLM read on how the day is going (planner). Each piece
is owned by its own feature/service; this just assembles them.

The synopsis is the only LLM call and it can be slow, so the deterministic
sections are sent first as one message and the synopsis follows when it's ready.

Like the router, it's handed its sibling features after they're built (agenda +
habits), since those are constructed elsewhere in the composition root.
"""

import asyncio
import html
from datetime import date, datetime
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from agenda_handlers import AgendaHandlers
from gcal import GCal
from habit_handlers import HabitHandlers
from planner import Planner, day_type

_TZ = ZoneInfo("Asia/Jerusalem")


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

    # --- Command ---

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return

        # Calendar is optional — a snapshot without it is still useful.
        try:
            events = await asyncio.to_thread(self.gcal.get_today_events)
            events_text = self.gcal.format_events(events)
        except Exception:
            events_text = "Calendar unavailable."

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

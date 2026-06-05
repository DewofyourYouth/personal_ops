"""Digest feature — the daily + weekly AI reviews.

A feature class (same shape as the other handlers): built with the bot and the
domain services it needs; its commands are methods that self-register via
`register(app)`. It owns digest rendering (markdown→Telegram HTML), persistence
to the Obsidian `digests/` folder, and the "which day does this digest cover"
logic.

The scheduled daily/weekly runs (`run_scheduled_daily`, `run_weekly`) are
exposed as methods; bot.py wraps them in thin module-level functions for the
scheduler, because the persistent job store needs picklable callables (a bound
method holding the Bot isn't picklable).
"""

import html
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from baseline_tracker import Baseline
from context import Context
from logs import Logs
from planner import Planner

_TZ = ZoneInfo("Asia/Jerusalem")


class DigestHandlers:
    def __init__(
        self,
        bot: Bot,
        planner: Planner,
        baseline: Baseline,
        logs: Logs,
        context: Context,
        shabbat,
        allowed_user: int,
    ) -> None:
        self.bot = bot
        self.planner = planner
        self.baseline = baseline
        self.logs = logs
        self.shabbat = shabbat
        self.allowed_user = allowed_user
        self._template = context.dir / "templates" / "digest-template.md"
        self._dir = context.dir / "digests"

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("digest", self.cmd_digest))
        app.add_handler(CommandHandler("daily", self.cmd_daily_digest))
        app.add_handler(CommandHandler("d", self.cmd_daily_digest))

    # --- Rendering + persistence ---

    @staticmethod
    def _to_html(text: str) -> str:
        lines = []
        for line in text.splitlines():
            line = re.sub(r"^#{1,3}\s*", "", line)
            if line.strip() == "---":
                lines.append("")
                continue
            # escape plain text segments, preserve bold/italic as HTML tags
            result = ""
            last = 0
            for m in re.finditer(r"\*\*(.+?)\*\*|\*(.+?)\*", line):
                result += html.escape(line[last : m.start()])
                if m.group(1) is not None:
                    result += f"<b>{html.escape(m.group(1))}</b>"
                else:
                    result += f"<i>{html.escape(m.group(2))}</i>"
                last = m.end()
            result += html.escape(line[last:])
            lines.append(result)
        return "\n".join(lines).strip()

    def _save(self, text: str, label: str = "digest") -> None:
        self._dir.mkdir(exist_ok=True)
        now = datetime.now(_TZ)
        template = (
            self._template.read_text()
            if self._template.exists()
            else '---\ntitle:\ngenerated: "{{DATETIME}}"\ntype: digest\n---\n'
        )
        filled = template.replace("{{DATETIME}}", now.isoformat(timespec="seconds"))
        date_str = now.date().isoformat()
        filled = filled.replace("title:", f"title: {date_str} {label}")
        path = self._dir / f"{date_str}-{label}.md"
        path.write_text(filled + "\n" + text + "\n")

    @staticmethod
    def _target_date() -> date:
        now = datetime.now(_TZ)
        # Before 6am counts as end of the previous day, not start of the new one
        if now.hour < 6:
            return date.today() - timedelta(days=1)
        return date.today()

    # --- Commands ---

    async def cmd_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        await update.message.reply_text("🔍 Generating digest…")
        try:
            self.baseline.compute_and_save_weekly(self.logs)
            text = await self.planner.digest()
            self._save(text, label="digest")
            await update.message.reply_text(self._to_html(text), parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Digest failed: {e}")

    async def cmd_daily_digest(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if update.effective_user.id != self.allowed_user:
            return
        arg = " ".join(context.args).strip().lower() if context.args else ""
        if arg in ("yesterday", "y"):
            target = date.today() - timedelta(days=1)
        else:
            target = self._target_date()

        label = "today" if target == date.today() else str(target)
        await update.message.reply_text(f"🔍 Generating daily digest for {label}…")
        try:
            text = await self.planner.daily_digest(target_date=target)
            self._save(text, label="daily")
            await update.message.reply_text(self._to_html(text), parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Daily digest failed: {e}")

    # --- Scheduled runs (wrapped by bot.py for the scheduler) ---

    async def run_scheduled_daily(self) -> None:
        if self.shabbat.quiet_now():
            return
        try:
            text = await self.planner.daily_digest(target_date=self._target_date())
            self._save(text, label="daily")
            await self.bot.send_message(
                chat_id=self.allowed_user,
                text=f"🌙 <b>Daily digest:</b>\n\n{self._to_html(text)}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def run_weekly(self) -> None:
        if self.shabbat.quiet_now():
            return
        try:
            self.baseline.compute_and_save_weekly(self.logs)
            text = await self.planner.digest()
            self._save(text, label="weekly-digest")
            await self.bot.send_message(
                chat_id=self.allowed_user,
                text=f"📋 <b>Weekly digest:</b>\n\n{self._to_html(text)}",
                parse_mode="HTML",
            )
        except Exception:
            pass

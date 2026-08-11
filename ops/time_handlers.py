"""Time tracking — a tracking-domain plugin.

Feature class: built with the bot + logs + a TimeTracker, `/timerstart` etc. are methods,
self-registers via `register(app)`, and satisfies `Trackable` via `summary(days)`.

Two capture styles, both fully deterministic (no LLM calls anywhere in this module):
  - a live timer (`/timerstart`, `/timerstop`, `/timerstatus`, `/timercancel`), backed by
    `time_tracker.py`'s `time_running` table for the in-progress state;
  - a retrospective `time:` prefix log (`time: 1h30m client call #acme`), owned end-to-end
    via `handle_classified_text` — the same plugin-owns-its-tag pattern grocery.py uses,
    rather than a special case inside text_router.py.

Both paths converge on the same completed-entry format once logged: a `#time` entry whose
content is `{description} — {duration} [project: {project}]`, parsed back out by
`logs._parse_time_entry` (the single home for that parse, mirroring `_parse_macros`).
"""

import html
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

import voice
from logs import TZ, Logs, _parse_time_entry
from tg_common import mono_table, send_long


REPORT_PERIOD_DAYS = {"week": 7, "month": 30, "quarter": 90, "year": 365}

_PROJECT_TAG_RE = re.compile(r"#([\w-]+)")

# A leading duration on a manual `time:` entry: "2h", "1h30m", "1.5h", "45m", "90 min",
# "2 hours". An explicit unit is required — a bare leading number is never read as a
# duration, so a description that happens to start with a digit is never misparsed.
_DURATION_PREFIX_RE = re.compile(
    r"^\s*(?:(?P<h>\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?"
    r"\s*(?:(?P<m>\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?)?\b[\s,:-]*",
    re.IGNORECASE,
)


def _parse_duration_prefix(text: str) -> tuple[float, str] | None:
    """Pull a leading duration off a manual time: entry, returning
    (minutes, remaining_description). None if no duration is found at the start,
    or nothing is left to describe what the time was spent on."""
    m = _DURATION_PREFIX_RE.match(text)
    if not m or (not m.group("h") and not m.group("m")):
        return None
    hours = float(m.group("h") or 0)
    minutes = float(m.group("m") or 0)
    remainder = text[m.end() :].strip()
    if not remainder:
        return None
    return hours * 60 + minutes, remainder


def _parse_project(text: str) -> tuple[str, str]:
    """Pull a #project tag out of free text, returning (text_without_tag,
    normalized_project). ("", text) unchanged and "" project if none found."""
    m = _PROJECT_TAG_RE.search(text)
    if not m:
        return text.strip(), ""
    project = m.group(1).lower()
    cleaned = text[: m.start()] + text[m.end() :]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, project


def _format_duration(minutes: float) -> str:
    """150 -> '2h 30m', 45 -> '45m', 60 -> '1h'."""
    total = round(minutes)
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _time_log_content(description: str, minutes: float, project: str = "") -> str:
    """The entry stored in the log once a duration is captured, parseable back
    out by logs._parse_time_entry."""
    suffix = f" [project: {project}]" if project else ""
    return f"{description} — {_format_duration(minutes)}{suffix}"


def _project_suffix(project: str) -> str:
    return f" [{project}]" if project else ""


def _time_report(entries: list[dict], period: str, end: date) -> str:
    """Build a rolling time report: total, breakdown by project, and an
    itemized entry list. Modeled on food_handlers._macros_report."""
    days = REPORT_PERIOD_DAYS[period]
    start = end - timedelta(days=days - 1)
    lines = [
        f"⏱ <b>Time — past {html.escape(period)}</b>",
        f"<i>{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')} ({days} days)</i>",
    ]

    parsed = [(e, _parse_time_entry(e["content"])) for e in entries]
    parsed = [(e, m) for e, m in parsed if m is not None]
    if not parsed:
        lines.extend(["", "No time was logged in this period."])
        return "\n".join(lines)

    total_minutes = sum(m["minutes"] for _, m in parsed)
    logged_days = len({e["date"] for e, _ in parsed})

    lines.append("")
    lines.append(
        f"<b>Total:</b> {_format_duration(total_minutes)} · "
        f"logged on <b>{logged_days}/{days}</b> days · "
        f"<b>{len(parsed)}</b> entries"
    )

    by_project: dict[str, float] = defaultdict(float)
    for _, m in parsed:
        by_project[m["project"] or "(no project)"] += m["minutes"]
    if by_project:
        lines.extend(["", "<b>By project</b>"])
        ranked = sorted(by_project.items(), key=lambda kv: -kv[1])
        lines.append(
            mono_table(
                ["Project", "Time"],
                [[html.escape(p), _format_duration(mins)] for p, mins in ranked],
            )
        )

    lines.extend(["", "<b>Entries</b>"])
    limit = 25
    # Most recent first — the itemized list is for a quick "what did I actually do" scan.
    recent = sorted(parsed, key=lambda em: em[0]["ts"], reverse=True)
    for e, m in recent[:limit]:
        d = e["date"]
        t = e["ts"][11:16]
        label = e["content"].split(" — ", 1)[0].strip()
        if len(label) > 80:
            label = label[:77].rstrip() + "…"
        lines.append(
            f"• {d} <code>{t}</code> {html.escape(label)} "
            f"— {_format_duration(m['minutes'])}{_project_suffix(html.escape(m['project']))}"
        )
    if len(recent) > limit:
        lines.append(f"• …and {len(recent) - limit} more entries")

    return "\n".join(lines)


class TimeHandlers:
    classification_tags = [
        {
            "tag": "time",
            "description": (
                "a block of time spent on an activity or project, reported after the "
                "fact with an explicit duration (e.g. '2h client call') — not a request "
                "to start or stop a live timer"
            ),
        }
    ]

    def __init__(self, bot: Bot, logs: Logs, time_tracker, allowed_user: int) -> None:
        self.bot = bot
        self.logs = logs
        self.time_tracker = time_tracker
        self.allowed_user = allowed_user

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("timerstart", self.cmd_timer_start))
        app.add_handler(CommandHandler("timerstop", self.cmd_timer_stop))
        app.add_handler(CommandHandler("timerstatus", self.cmd_timer_status))
        app.add_handler(CommandHandler("timercancel", self.cmd_timer_cancel))
        app.add_handler(CommandHandler("time", self.cmd_time_today))
        app.add_handler(CommandHandler("timereport", self.cmd_time_report))

    # --- Trackable capability ---

    def summary(self, days: int) -> str:
        """How much time was logged over the window — for the digest / eval."""
        start = date.today() - timedelta(days=max(days, 1) - 1)
        rows = self.logs.db.entries_for_range(start, date.today())
        entries = [
            m
            for r in rows
            if r["tag"] == "time" and (m := _parse_time_entry(r["content"]))
        ]
        if not entries:
            return ""
        total = sum(m["minutes"] for m in entries)
        return f"Time: {_format_duration(total)} logged across {len(entries)} entries."

    # --- time: prefix (retrospective log) ---

    async def handle_classified_text(self, tag: str, content: str, reply) -> bool:
        if tag != "time":
            return False
        parsed = _parse_duration_prefix(content)
        if not parsed:
            await reply(
                "Couldn't find a duration at the start. Try "
                "<code>time: 1h30m description #project</code>.",
                parse_mode="HTML",
            )
            return True
        minutes, remainder = parsed
        description, project = _parse_project(remainder)
        if not description:
            await reply("What were you doing? Add a description after the duration.")
            return True
        log_content = _time_log_content(description, minutes, project)
        self.logs.write("time", log_content)
        await reply(
            f"⏱ Logged: {html.escape(description)} — {_format_duration(minutes)}"
            f"{_project_suffix(html.escape(project))}",
            parse_mode="HTML",
        )
        return True

    # --- Live timer ---

    async def cmd_timer_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        chat_id = update.effective_chat.id
        already = self.time_tracker.running(chat_id)
        if already:
            await update.message.reply_text(
                f"⏱ Already timing: {already['description']}"
                f"{_project_suffix(already['project'])}. "
                "Use /timerstop or /timercancel first."
            )
            return
        text = " ".join(context.args).strip() if context.args else ""
        if not text:
            await update.message.reply_text(
                "Usage: <code>/timerstart description #project</code>",
                parse_mode="HTML",
            )
            return
        description, project = _parse_project(text)
        if not description:
            await update.message.reply_text(
                "Usage: <code>/timerstart description #project</code>",
                parse_mode="HTML",
            )
            return
        self.time_tracker.start(chat_id, description, project)
        await update.message.reply_text(
            f"⏱ Timer started: {html.escape(description)}"
            f"{_project_suffix(html.escape(project))}",
            parse_mode="HTML",
        )

    async def cmd_timer_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        chat_id = update.effective_chat.id
        result = self.time_tracker.stop(chat_id)
        if result is None:
            await update.message.reply_text(
                "No timer running. Start one with /timerstart."
            )
            return
        if result["minutes"] < 1:
            await update.message.reply_text("That was under a minute — not logged.")
            return
        log_content = _time_log_content(
            result["description"], result["minutes"], result["project"]
        )
        self.logs.write("time", log_content)
        await update.message.reply_text(
            f"⏱ Logged: {html.escape(result['description'])} — "
            f"{_format_duration(result['minutes'])}"
            f"{_project_suffix(html.escape(result['project']))}",
            parse_mode="HTML",
        )

    async def cmd_timer_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        chat_id = update.effective_chat.id
        running = self.time_tracker.running(chat_id)
        if not running:
            await update.message.reply_text("No timer running.")
            return
        started = datetime.fromisoformat(running["start_ts"])
        elapsed = (datetime.now(TZ) - started).total_seconds() / 60
        await update.message.reply_text(
            f"⏱ Running: {html.escape(running['description'])}"
            f"{_project_suffix(html.escape(running['project']))} — "
            f"{_format_duration(elapsed)} so far",
            parse_mode="HTML",
        )

    async def cmd_timer_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        chat_id = update.effective_chat.id
        cancelled = self.time_tracker.cancel(chat_id)
        if not cancelled:
            await update.message.reply_text("No timer running.")
            return
        await update.message.reply_text(
            f"⏱ Discarded: {html.escape(cancelled['description'])} (not logged).",
            parse_mode="HTML",
        )

    # --- Reports ---

    async def cmd_time_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        entries = [e for e in self.logs.read_today() if e.get("tag") == "time"]
        if not entries:
            await update.message.reply_text(
                "Nothing logged yet today. Use <code>time: 1h description</code> or "
                "<code>/timerstart description</code>.",
                parse_mode="HTML",
            )
            return
        lines = ["⏱ <b>Today's time log:</b>\n"]
        total = 0.0
        for e in entries:
            t = e["ts"][11:16]
            lines.append(f"<code>{t}</code> {html.escape(e['content'])}")
            m = _parse_time_entry(e["content"])
            if m:
                total += m["minutes"]
        if total:
            lines.append(f"\n<b>Total:</b> {_format_duration(total)}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_time_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/timereport week|month|quarter|year — rolling time totals, by-project
        breakdown, and an itemized entry list."""
        if update.effective_user.id != self.allowed_user:
            return
        period = " ".join(context.args).strip().lower() if context.args else ""
        if period not in REPORT_PERIOD_DAYS:
            await update.message.reply_text(
                "Usage: <code>/timereport week</code>, <code>/timereport month</code>, "
                "<code>/timereport quarter</code>, or <code>/timereport year</code>.",
                parse_mode="HTML",
            )
            return

        end = date.today()
        start = end - timedelta(days=REPORT_PERIOD_DAYS[period] - 1)
        entries = [
            dict(row)
            for row in self.logs.db.entries_for_range(start, end)
            if row["tag"] == "time"
        ]
        report = _time_report(entries, period, end)
        msg = await send_long(update.message.reply_text, report, parse_mode="HTML")
        await voice.offer(self.bot, msg)

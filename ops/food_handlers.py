"""Food — a tracking-domain plugin.

Feature class: built with the bot + logs, `/food` is a method, self-registers via
`register(app)`, and satisfies `Trackable` via `summary(days)`.

Food is logged with the `food:` / `ate:` prefixes (the dispatcher enriches the
entry with parsed macros at write time); this plugin owns the read side.
"""

import html
import re
from datetime import date, timedelta

import anthropic
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from logs import Logs

# The macro summary line written by text_router._food_log_content, e.g.
# "lasagna and salad — ~480 kcal, 24g protein, 21g fat, 47g carbs".
_MACRO_RE = re.compile(
    r"~?\s*([\d.]+)\s*kcal,\s*([\d.]+)\s*g\s*protein,\s*"
    r"([\d.]+)\s*g\s*fat,\s*([\d.]+)\s*g\s*carbs",
    re.IGNORECASE,
)


def _parse_macros(content: str) -> dict | None:
    """Pull kcal/protein/fat/carbs out of a food entry's summary line, or None if the
    entry has no macro estimate (e.g. one logged raw when the estimator was unavailable)."""
    m = _MACRO_RE.search(content)
    if not m:
        return None
    kcal, protein, fat, carbs = (float(g) for g in m.groups())
    return {"kcal": kcal, "protein_g": protein, "fat_g": fat, "carbs_g": carbs}


def _macro_totals(contents: list[str]) -> dict | None:
    """Sum the macros across food entries. None if none of them carry an estimate."""
    parsed = [m for c in contents if (m := _parse_macros(c))]
    if not parsed:
        return None
    return {
        k: sum(p[k] for p in parsed) for k in ("kcal", "protein_g", "fat_g", "carbs_g")
    }


def _fmt(n: float) -> str:
    """Drop a trailing .0 so 24.0 → '24' but 21.5 stays '21.5'."""
    return str(int(n)) if float(n).is_integer() else str(round(n, 1))


class FoodHandlers:
    classification_tags = [
        {
            "tag": "food",
            "description": "a meal or food consumed, to log with a nutrition estimate",
        }
    ]

    def __init__(self, bot: Bot, logs: Logs, allowed_user: int) -> None:
        self.bot = bot
        self.logs = logs
        self.allowed_user = allowed_user

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("food", self.cmd_food))
        app.add_handler(CommandHandler("foodlog", self.cmd_food))
        app.add_handler(CommandHandler("foodaudit", self.cmd_food_audit))

    # --- Trackable capability ---

    def summary(self, days: int) -> str:
        """How consistently food was logged over the window — for the digest / eval."""
        start = date.today() - timedelta(days=max(days, 1) - 1)
        rows = self.logs.db.entries_for_range(start, date.today())
        food = [r for r in rows if r["tag"] == "food"]
        if not food:
            return ""
        days_with = len({r["date"] for r in food})
        return f"Food: {len(food)} entries logged on {days_with}/{days} days."

    # --- Handlers ---

    async def cmd_food(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user:
            return
        entries = [e for e in self.logs.read_today() if e.get("tag") == "food"]
        if not entries:
            await update.message.reply_text(
                "Nothing logged yet today. Use <code>food: what you ate</code>.",
                parse_mode="HTML",
            )
            return
        lines = ["🍽 <b>Today's food log:</b>\n"]
        for e in entries:
            t = e["ts"][11:16]
            lines.append(f"<code>{t}</code> {html.escape(e['content'])}")

        totals = _macro_totals([e["content"] for e in entries])
        if totals:
            lines.append(
                "\n<b>Totals (approx)</b>\n"
                "<table><tr><th>kcal</th><th>Protein</th><th>Fat</th><th>Carbs</th></tr>"
                f"<tr><td>~{_fmt(totals['kcal'])}</td>"
                f"<td>{_fmt(totals['protein_g'])}g</td>"
                f"<td>{_fmt(totals['fat_g'])}g</td>"
                f"<td>{_fmt(totals['carbs_g'])}g</td></tr></table>"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_food_audit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/foodaudit — instrument food logging patterns: compare log rates for
        healthy vs indulgent meals to separate friction from avoidance."""
        if update.effective_user.id != self.allowed_user:
            return
        await update.message.reply_text("🔍 Analysing food log patterns…")

        days = 30
        start = date.today() - timedelta(days=days - 1)
        rows = self.logs.db.entries_for_range(start, date.today())
        food_entries = [r for r in rows if r["tag"] == "food"]

        if len(food_entries) < 3:
            await update.message.reply_text(
                "Not enough food entries to audit (need at least 3). Keep logging!"
            )
            return

        # Ask Haiku to classify each entry as healthy/indulgent/mixed.
        entries_text = "\n".join(
            f"{r['date']} {r['ts'][11:16]}: {r['content']}" for r in food_entries
        )
        client = anthropic.AsyncAnthropic()
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=(
                    "You are a nutrition analyst. Classify each food log entry as one of: "
                    "healthy, indulgent, or mixed. Return JSON: "
                    '{"classifications": [{"date": "YYYY-MM-DD", "time": "HH:MM", '
                    '"label": "healthy|indulgent|mixed"}]}'
                ),
                messages=[{"role": "user", "content": entries_text}],
            )
            import json

            data = json.loads(resp.content[0].text)
            classifications = data.get("classifications", [])
        except Exception as e:
            await update.message.reply_text(f"Audit failed: {e}")
            return

        counts: dict[str, int] = {"healthy": 0, "indulgent": 0, "mixed": 0}
        for c in classifications:
            label = c.get("label", "mixed")
            counts[label] = counts.get(label, 0) + 1

        total = sum(counts.values())
        if total == 0:
            await update.message.reply_text("Classification returned no results.")
            return

        lines = [
            f"🍽 <b>Food log audit — last {days} days</b>",
            f"Total entries: <b>{len(food_entries)}</b>",
            "",
            f"🥗 Healthy:   <b>{counts['healthy']}</b> ({100*counts['healthy']//total}%)",
            f"🍰 Indulgent: <b>{counts['indulgent']}</b> ({100*counts['indulgent']//total}%)",
            f"🍱 Mixed:     <b>{counts['mixed']}</b> ({100*counts['mixed']//total}%)",
            "",
        ]

        days_with_food = len({r["date"] for r in food_entries})
        lines.append(
            f"Logged on <b>{days_with_food}/{days}</b> days "
            f"({100*days_with_food//days}% coverage)."
        )
        lines.append("")
        lines.append(
            "<i>If indulgent meals are under-represented vs your actual eating, "
            "that's avoidance. If the ratio looks right, logging friction is the issue.</i>"
        )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

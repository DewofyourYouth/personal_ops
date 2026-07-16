"""Food — a tracking-domain plugin.

Feature class: built with the bot + logs, `/food` is a method, self-registers via
`register(app)`, and satisfies `Trackable` via `summary(days)`.

Food is logged with the `food:` / `ate:` prefixes (the dispatcher enriches the
entry with parsed macros at write time); this plugin owns the read side.
"""

import html
from collections import defaultdict
from datetime import date, timedelta

import anthropic
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from logs import Logs, _parse_macros
from tg_common import mono_table, send_long


MACRO_PERIOD_DAYS = {"week": 7, "month": 30, "quarter": 90, "year": 365}


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


def _food_label(content: str) -> str:
    """Return the meal description, without its macro estimate/itemization."""
    first_line = content.splitlines()[0].strip()
    return first_line.split(" — ", 1)[0].strip() or "Unlabelled food"


def _macros_report(
    entries: list[dict],
    negations: list[dict],
    period: str,
    end: date,
) -> str:
    """Build a rolling macro report from food entries and their retractions."""
    days = MACRO_PERIOD_DAYS[period]
    start = end - timedelta(days=days - 1)
    lines = [
        f"📊 <b>Macros — past {html.escape(period)}</b>",
        f"<i>{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')} ({days} days)</i>",
    ]
    if not entries:
        lines.extend(["", "No food was logged in this period."])
        return "\n".join(lines)

    negated_fraction: dict[int, float] = defaultdict(float)
    for n in negations:
        negated_fraction[n["ref_entry_id"]] += float(n["fraction"])

    # Keep the consumption summary consistent with macro retractions: a fully
    # retracted entry disappears; a partial one contributes a fractional count.
    consumed: dict[str, dict] = {}
    for entry in entries:
        fraction = max(0.0, 1.0 - negated_fraction[entry["id"]])
        if fraction <= 0:
            continue
        label = _food_label(entry["content"])
        key = label.casefold()
        if key not in consumed:
            consumed[key] = {"label": label, "count": 0.0}
        consumed[key]["count"] += fraction

    parsed = [(e["id"], _parse_macros(e["content"])) for e in entries]
    estimated_count = sum(1 for _, macros in parsed if macros is not None)
    totals = {
        key: sum(macros[key] for _, macros in parsed if macros is not None)
        for key in ("kcal", "protein_g", "fat_g", "carbs_g")
    }
    for n in negations:
        totals["kcal"] += n["kcal_delta"]
        totals["protein_g"] += n["protein_delta"]
        totals["fat_g"] += n["fat_delta"]
        totals["carbs_g"] += n["carbs_delta"]

    # Averages reflect the normal Sunday–Thursday week. Period totals remain
    # inclusive of Friday/Saturday so the report still accounts for all food.
    average_entry_ids = {
        e["id"]
        for e in entries
        if date.fromisoformat(e["date"]).weekday() in {6, 0, 1, 2, 3}
    }
    average_totals = {
        key: sum(
            macros[key]
            for entry_id, macros in parsed
            if entry_id in average_entry_ids and macros is not None
        )
        for key in ("kcal", "protein_g", "fat_g", "carbs_g")
    }
    for n in negations:
        if n["ref_entry_id"] in average_entry_ids:
            average_totals["kcal"] += n["kcal_delta"]
            average_totals["protein_g"] += n["protein_delta"]
            average_totals["fat_g"] += n["fat_delta"]
            average_totals["carbs_g"] += n["carbs_delta"]

    average_days = sum(
        1
        for offset in range(days)
        if (start + timedelta(days=offset)).weekday() in {6, 0, 1, 2, 3}
    )
    average_logged_days = len(
        {
            e["date"]
            for e in entries
            if date.fromisoformat(e["date"]).weekday() in {6, 0, 1, 2, 3}
        }
    )

    logged_days = len({e["date"] for e in entries})
    lines.append("")
    if estimated_count:
        logged_divisor = average_logged_days or 1
        lines.append(
            mono_table(
                ["", "kcal", "Protein", "Fat", "Carbs"],
                [
                    [
                        "Total",
                        f"~{_fmt(totals['kcal'])}",
                        f"{_fmt(totals['protein_g'])}g",
                        f"{_fmt(totals['fat_g'])}g",
                        f"{_fmt(totals['carbs_g'])}g",
                    ],
                    [
                        "Sun–Thu/day",
                        f"~{_fmt(average_totals['kcal'] / average_days)}",
                        f"{_fmt(average_totals['protein_g'] / average_days)}g",
                        f"{_fmt(average_totals['fat_g'] / average_days)}g",
                        f"{_fmt(average_totals['carbs_g'] / average_days)}g",
                    ],
                    [
                        "Sun–Thu/logged",
                        f"~{_fmt(average_totals['kcal'] / logged_divisor)}",
                        f"{_fmt(average_totals['protein_g'] / logged_divisor)}g",
                        f"{_fmt(average_totals['fat_g'] / logged_divisor)}g",
                        f"{_fmt(average_totals['carbs_g'] / logged_divisor)}g",
                    ],
                ],
            )
        )
    else:
        lines.append("No macro estimates were available for the food logged.")

    lines.extend(
        [
            "",
            f"Logged on <b>{logged_days}/{days}</b> days · "
            f"<b>{len(entries)}</b> food entries · "
            f"macros on <b>{estimated_count}/{len(entries)}</b>",
        ]
    )

    if consumed:
        lines.extend(["", "<b>Consumed (net)</b>"])
        ranked = sorted(
            consumed.values(),
            key=lambda item: (-item["count"], item["label"].casefold()),
        )
        limit = 25
        for item in ranked[:limit]:
            label = item["label"]
            if len(label) > 100:
                label = label[:97].rstrip() + "…"
            lines.append(f"• {html.escape(label)} × {_fmt(item['count'])}")
        if len(ranked) > limit:
            lines.append(f"• …and {len(ranked) - limit} other meal types")

    return "\n".join(lines)


class FoodHandlers:
    classification_tags = [
        {
            "tag": "food",
            "description": (
                "a meal or food ALREADY EATEN or being eaten now, reported as a log "
                "event — not a narrative mention, order, complaint, or third-person "
                "mention of food (e.g. 'ordered pizza and it arrived cold' is NOT "
                "food; only classify as food if the text reports something consumed)"
            ),
        }
    ]

    def __init__(self, bot: Bot, logs: Logs, allowed_user: int) -> None:
        self.bot = bot
        self.logs = logs
        self.allowed_user = allowed_user

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("food", self.cmd_food))
        app.add_handler(CommandHandler("foodlog", self.cmd_food))
        app.add_handler(CommandHandler("macros", self.cmd_macros))
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

        totals = self.logs.food_totals_for_entries(entries)
        if totals:
            lines.append("\n<b>Totals (approx)</b>")
            lines.append(
                mono_table(
                    ["kcal", "Protein", "Fat", "Carbs"],
                    [
                        [
                            f"~{_fmt(totals['kcal'])}",
                            f"{_fmt(totals['protein_g'])}g",
                            f"{_fmt(totals['fat_g'])}g",
                            f"{_fmt(totals['carbs_g'])}g",
                        ]
                    ],
                )
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_macros(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/macros week|month|quarter|year — rolling food and macro results."""
        if update.effective_user.id != self.allowed_user:
            return
        period = " ".join(context.args).strip().lower() if context.args else ""
        if period not in MACRO_PERIOD_DAYS:
            await update.message.reply_text(
                "Usage: <code>/macros week</code>, <code>/macros month</code>, "
                "<code>/macros quarter</code>, or <code>/macros year</code>.",
                parse_mode="HTML",
            )
            return

        end = date.today()
        start = end - timedelta(days=MACRO_PERIOD_DAYS[period] - 1)
        entries = [
            dict(row)
            for row in self.logs.db.entries_for_range(start, end)
            if row["tag"] == "food"
        ]
        negations = self.logs.db.food_negations_for_entry_ids(
            [entry["id"] for entry in entries]
        )
        report = _macros_report(entries, [dict(n) for n in negations], period, end)
        await send_long(update.message.reply_text, report, parse_mode="HTML")

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
            f"🥗 Healthy:   <b>{counts['healthy']}</b> ({100 * counts['healthy'] // total}%)",
            f"🍰 Indulgent: <b>{counts['indulgent']}</b> ({100 * counts['indulgent'] // total}%)",
            f"🍱 Mixed:     <b>{counts['mixed']}</b> ({100 * counts['mixed'] // total}%)",
            "",
        ]

        days_with_food = len({r["date"] for r in food_entries})
        lines.append(
            f"Logged on <b>{days_with_food}/{days}</b> days "
            f"({100 * days_with_food // days}% coverage)."
        )
        lines.append("")
        lines.append(
            "<i>If indulgent meals are under-represented vs your actual eating, "
            "that's avoidance. If the ratio looks right, logging friction is the issue.</i>"
        )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

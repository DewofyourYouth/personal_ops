"""weight.py — Wegovy weight-loss progress reporting.

The weight readings already live in the `metrics` table (logged via `metric: weight`
and the one-time Apple Health import). This module is the deterministic reporting layer
on top of them — the native replacement for the Obsidian dataview note: latest weigh-ins,
total lost since the first injection, and weekly averages with week-over-week change.

The Wegovy baseline (start weight + date) are constants here rather than config — this is
a personal tool and they are fixed historical facts. Change them here if they ever need to.
"""

from datetime import date

WEGOVY_START_WEIGHT = 103.5  # kg, the weigh-in at the first injection
WEGOVY_START_DATE = date(2025, 11, 11)
KG_TO_LB = 2.20462


class Weight:
    def __init__(self, db):
        self.db = db

    def _per_day(self, since: date | None = None) -> list[tuple[str, float]]:
        """Latest weight reading per calendar day, ascending by date.

        Collapses multiple same-day readings (e.g. a metric plus a habit weigh-in) to
        the last one, matching the one-value-per-day model the Obsidian note assumed.
        """
        rows = self.db.metrics_for_range(since or date(2000, 1, 1), date.today())
        per_day: dict[str, float] = {}
        for r in rows:  # rows come ordered by (date, ts), so the last write per day wins
            if r["key"] != "weight":
                continue
            try:
                per_day[r["date"]] = float(r["value"])
            except (ValueError, TypeError):
                continue
        return sorted(per_day.items())

    def latest(self, n: int = 5) -> list[dict]:
        """The n most recent weigh-in days, newest first, with deltas vs the Wegovy start."""
        days = self._per_day()
        out = []
        for d, kg in reversed(days[-n:]):
            out.append(
                {
                    "date": d,
                    "kg": kg,
                    "delta_since_start": round(kg - WEGOVY_START_WEIGHT, 1),
                    "kg_lost": round(WEGOVY_START_WEIGHT - kg, 1),
                }
            )
        return out

    def total_lost(self) -> dict | None:
        """Total lost since the Wegovy start weight, in kg and lb. None if no data."""
        days = self._per_day()
        if not days:
            return None
        _, latest_kg = days[-1]
        lost_kg = WEGOVY_START_WEIGHT - latest_kg
        return {
            "current_kg": latest_kg,
            "lost_kg": round(lost_kg, 1),
            "lost_lb": round(lost_kg * KG_TO_LB, 1),
        }

    def weekly_averages(self) -> list[dict]:
        """Per-ISO-week average weight since the Wegovy start, newest first.

        Each row carries the average, the delta from the start weight, and the change
        versus the previous week (the signal that shows whether loss is still happening).
        """
        days = self._per_day(since=WEGOVY_START_DATE)
        buckets: dict[str, list[float]] = {}
        for d, kg in days:
            y, w, _ = date.fromisoformat(d).isocalendar()
            buckets.setdefault(f"{y}-W{w:02d}", []).append(kg)

        rows = []
        prev_avg = None
        for week in sorted(buckets):
            avg = sum(buckets[week]) / len(buckets[week])
            rows.append(
                {
                    "week": week,
                    "avg": round(avg, 1),
                    "delta_since_start": round(avg - WEGOVY_START_WEIGHT, 1),
                    "delta_vs_prev": round(avg - prev_avg, 2)
                    if prev_avg is not None
                    else None,
                }
            )
            prev_avg = avg
        rows.reverse()
        return rows

    def format_for_telegram(self, weeks: int = 6) -> str:
        total = self.total_lost()
        if not total:
            return "No weight readings logged yet. Log one with: <code>metric: weight 94.3</code>"

        def signed(v, unit=""):
            return f"+{v}{unit}" if v > 0 else f"{v}{unit}"

        lines = [
            f"⚖️ <b>Weight — Wegovy progress</b>",
            f"Start {WEGOVY_START_WEIGHT} kg ({WEGOVY_START_DATE.isoformat()})\n",
            f"<b>Current:</b> {total['current_kg']} kg",
            f"<b>Total lost:</b> {total['lost_kg']} kg ({total['lost_lb']} lb)\n",
        ]

        lines.append("<b>Latest weigh-ins</b>")
        for r in self.latest(5):
            lines.append(f"<code>{r['date']}</code>  {r['kg']} kg  ({r['kg_lost']} lost)")

        weekly = self.weekly_averages()
        if weekly:
            lines.append("\n<b>Weekly average</b>  (Δ vs prev week)")
            for r in weekly[:weeks]:
                vs_prev = "—" if r["delta_vs_prev"] is None else signed(r["delta_vs_prev"])
                lines.append(
                    f"<code>{r['week']}</code>  {r['avg']} kg  ({vs_prev})"
                )

        return "\n".join(lines)

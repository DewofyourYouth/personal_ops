"""weight.py — Wegovy weight-loss progress reporting.

The weight readings already live in the `metrics` table (logged via `metric: weight`
and the one-time Apple Health import). This module is the deterministic reporting layer
on top of them — the native replacement for the Obsidian dataview note: latest weigh-ins,
total lost since the first injection, and weekly averages with week-over-week change.

The Wegovy baseline (start weight + date) are constants here rather than config — this is
a personal tool and they are fixed historical facts. Change them here if they ever need to.
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEGOVY_START_WEIGHT = 103.5  # kg, the documented weigh-in at the first injection
WEGOVY_START_DATE = date(2025, 11, 11)
KG_TO_LB = 2.20462
RATE_WINDOW_DAYS = 28  # trailing window for the kg/week trend slope


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
        for (
            r
        ) in rows:  # rows come ordered by (date, ts), so the last write per day wins
            if r["key"] != "weight":
                continue
            try:
                per_day[r["date"]] = float(r["value"])
            except (ValueError, TypeError):
                continue
        return sorted(per_day.items())

    def injections(self) -> list[tuple[str, str]]:
        """Logged Wegovy injections as (date, dose) pairs, oldest first."""
        rows = self.db.entries_for_range(WEGOVY_START_DATE, date.today())
        return [
            (r["date"], (r["content"] or "").strip())
            for r in rows
            if r["tag"] == "injection"
        ]

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

    @staticmethod
    def _window_avg(days: list[tuple[str, float]], first: bool, n: int = 7) -> float:
        """Average of the first (or last) n per-day readings — a smoothed endpoint."""
        window = days[:n] if first else days[-n:]
        return sum(kg for _, kg in window) / len(window)

    def _rate_per_week(self, days: list[tuple[str, float]]) -> float | None:
        """Least-squares trend over the last RATE_WINDOW_DAYS, in kg/week.

        Negative = losing. More robust than a two-point delta because it uses every
        recent reading and isn't tied to arbitrary week boundaries. None if too sparse.
        """
        cutoff = date.today() - timedelta(days=RATE_WINDOW_DAYS)
        pts = [
            (date.fromisoformat(d).toordinal(), kg)
            for d, kg in days
            if date.fromisoformat(d) >= cutoff
        ]
        if len(pts) < 3:
            return None
        n = len(pts)
        mx = sum(x for x, _ in pts) / n
        my = sum(y for _, y in pts) / n
        var = sum((x - mx) ** 2 for x, _ in pts)
        if var == 0:
            return None
        cov = sum((x - mx) * (y - my) for x, y in pts)
        return (cov / var) * 7  # slope per day → per week

    def summary(self) -> dict | None:
        """Smoothed figures for the report and synopsis — cached per weigh-in date.

        The cache key is the latest weigh-in date, fetched cheaply. If figures were already
        computed for it they're returned as-is (no rescan/recompute); otherwise they're
        computed once and stored. Logging a new weight changes the key and invalidates it.
        """
        basis = self.db.max_weight_date()
        if not basis:
            return None
        cached = self.db.weight_cache_get(basis)
        if cached and cached["figures"]:
            return json.loads(cached["figures"])
        figures = self._compute_summary()
        if figures:
            self.db.cache_weight_figures(
                basis,
                datetime.now(ZoneInfo("Asia/Jerusalem")).isoformat(timespec="seconds"),
                json.dumps(figures),
            )
        return figures

    def _compute_summary(self) -> dict | None:
        """Compute the smoothed figures from raw readings (the cached payload).

        Endpoints are 7-day averages (not single noisy days); loss is anchored to the
        first-week average; rate is a trailing regression slope; loss is also expressed
        as % of body weight (the metric Wegovy outcomes are actually measured in).
        """
        days = self._per_day()
        if not days:
            return None
        wegovy_days = self._per_day(since=WEGOVY_START_DATE) or days
        start_avg = self._window_avg(wegovy_days, first=True)
        current_avg = self._window_avg(days, first=False)
        lost_kg = start_avg - current_avg
        rate = self._rate_per_week(days)
        return {
            "latest_date": days[-1][0],
            "latest_kg": days[-1][1],
            "documented_start_kg": WEGOVY_START_WEIGHT,
            "start_week_avg_kg": round(start_avg, 1),
            "current_7day_avg_kg": round(current_avg, 1),
            "lost_kg": round(lost_kg, 1),
            "lost_lb": round(lost_kg * KG_TO_LB, 1),
            "pct_of_bodyweight": round(100 * lost_kg / start_avg, 1),
            "rate_kg_per_week": round(rate, 2) if rate is not None else None,
            "readings": len(days),
        }

    def format_for_telegram(self, weeks: int = 6) -> str:
        s = self.summary()
        if not s:
            return "No weight readings logged yet. Log one with: <code>metric: weight 94.3</code>"

        def signed(v, unit=""):
            return f"+{v}{unit}" if v > 0 else f"{v}{unit}"

        if s["rate_kg_per_week"] is None:
            trend = "trend: not enough recent data"
        else:
            r = s["rate_kg_per_week"]
            arrow = "↓" if r < -0.05 else "↑" if r > 0.05 else "→"
            trend = f"trend: {arrow} {abs(r)} kg/week (last 4 wks)"

        lines = [
            "⚖️ <b>Weight progress</b>",
            f"{s['start_week_avg_kg']} kg (start wk) → {s['current_7day_avg_kg']} kg (7-day avg)",
            f"<b>Lost {s['lost_kg']} kg</b> ({s['lost_lb']} lb) · {s['pct_of_bodyweight']}% of body weight",
            f"{trend}\n",
        ]

        lines.append("<b>Latest weigh-ins</b> (raw daily — noisy)")
        for r in self.latest(5):
            lines.append(
                f"<code>{r['date']}</code>  {r['kg']} kg  ({r['kg_lost']} lost)"
            )

        weekly = self.weekly_averages()
        if weekly:
            lines.append("\n<b>Weekly average</b>  (Δ vs prev week)")
            for r in weekly[:weeks]:
                vs_prev = (
                    "—" if r["delta_vs_prev"] is None else signed(r["delta_vs_prev"])
                )
                lines.append(f"<code>{r['week']}</code>  {r['avg']} kg  ({vs_prev})")

        injections = self.injections()
        if injections:
            last_date, last_dose = injections[-1]
            lines.append(
                f"\n<b>Injections:</b> {len(injections)} logged · "
                f"latest {last_dose} on {last_date}"
            )

        return "\n".join(lines)

    def chart_png(self) -> bytes | None:
        """Render the Wegovy-era weight chart as PNG bytes (None if no data/plot fails).

        Daily readings as faint dots, a 7-day rolling average as the trend line, the
        start-week baseline as a reference, and injection dates marked — so a change in
        slope can be read against a dose change. matplotlib is imported lazily so the
        rest of the module has no hard dependency on it.
        """
        days = self._per_day(since=WEGOVY_START_DATE)
        if len(days) < 2:
            return None
        try:
            import io

            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
        except Exception:
            return None

        xs = [date.fromisoformat(d) for d, _ in days]
        ys = [kg for _, kg in days]

        # 7-day trailing rolling average over the per-day series.
        roll = []
        for i, d in enumerate(xs):
            window = [
                kg
                for x, kg in zip(xs, ys)
                if timedelta(0) <= (d - x) <= timedelta(days=6)
            ]
            roll.append(sum(window) / len(window))

        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.scatter(xs, ys, s=12, color="#c9c9c9", label="daily", zorder=2)
        ax.plot(xs, roll, color="#1f77b4", linewidth=2, label="7-day avg", zorder=3)
        ax.axhline(
            self._window_avg(days, first=True),
            color="#999",
            linestyle="--",
            linewidth=1,
            label="start-week avg",
        )

        for inj_date, dose in self.injections():
            try:
                x = date.fromisoformat(inj_date)
            except ValueError:
                continue
            ax.axvline(x, color="#2ca02c", alpha=0.25, linewidth=1, zorder=1)

        ax.set_title("Weight over time")
        ax.set_ylabel("kg")
        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper right", fontsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        fig.autofmt_xdate()
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120)
        plt.close(fig)
        return buf.getvalue()

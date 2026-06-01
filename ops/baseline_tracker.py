"""
baseline_tracker.py — Progressive compression of historical performance stats.

WHY THIS EXISTS
---------------
The weekly digest used to pass 7 days of raw log entries to the LLM. That works early on
but doesn't scale — eventually it means dumping every "habit: Yerushalmi" ever logged.
More importantly, we want the digest to say things like "you're functioning significantly
better than three months ago, even if it doesn't feel like it" — which requires longitudinal
context the raw logs can't provide.

HOW IT WORKS
------------
Stats are stored in three tiers in ops/log/baseline.json:

  Weekly   — last 8 weeks at full resolution (completion %, anchor %, wins, per-habit counts)
  Monthly  — weeks older than 8 weeks, averaged into monthly summaries
  Quarterly— months older than 12 months, averaged into quarterly summaries

Compression runs automatically each time compute_and_save_weekly() is called (Sunday night
when the weekly digest fires). The total number of data points stays bounded forever.

WHY NO NOTES ARE BAKED IN
--------------------------
Monthly and quarterly summaries are pure numbers — no AI-generated characterisations. This
preserves the ability to re-interpret history as more data arrives. A cached conclusion
("low energy period") would become load-bearing and resist revision. Raw numbers don't.
This also means a 7-year cycle would still be visible in 2033.

GOODHART'S LAW NOTE (Phase 2 — not yet implemented)
----------------------------------------------------
Completion % is a useful metric but becomes a target if users can game it by avoiding hard
commitments. The spec (BASELINE_SPEC.md) describes a 30-day calibration window per habit/
item before it enters the baseline. This is Phase 2, after we have real baseline data to
look at.
"""

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


class Baseline:
    WEEKLY_KEEP = 8    # weeks kept at full resolution before rolling into monthly
    MONTHLY_KEEP = 12  # months kept before rolling into quarterly

    def __init__(self, log_dir: str):
        self.path = Path(log_dir) / "baseline.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {"weekly": [], "monthly": [], "quarterly": []}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"weekly": [], "monthly": [], "quarterly": []}

    def save(self, data: dict):
        self.path.write_text(json.dumps(data, indent=2))

    def compute_and_save_weekly(self, logs) -> dict:
        """Snapshot this week's stats, compress old entries, save. Returns the new entry.

        Called at the end of each weekly digest run. Safe to call multiple times in a week —
        it replaces the existing entry for the current week rather than appending a duplicate.
        """
        data = self.load()
        entry = self._compute_week(logs)
        week_start = entry["week_start"]

        # Replace existing entry for this week if re-running
        data["weekly"] = [w for w in data["weekly"] if w["week_start"] != week_start]
        data["weekly"].append(entry)
        data["weekly"].sort(key=lambda w: w["week_start"])

        data = self._compress(data)
        self.save(data)
        return entry

    def format_for_prompt(self) -> str:
        """Render the full baseline history as markdown tables for the LLM prompt.

        Returns an empty string if there's no history yet (first week of use).
        The LLM gets numbers only — no cached interpretations. It draws its own conclusions
        each time, which means it can spot trends that weren't visible in earlier runs.
        """
        data = self.load()
        if not any(data.values()):
            return ""

        lines = ["## Historical baseline\n"]

        if data["weekly"]:
            lines.append("### Weekly (recent)")
            lines.append("| Week | Completion | Anchors | Wins | Habits |")
            lines.append("|------|------------|---------|------|--------|")
            for w in reversed(data["weekly"]):
                comp = f"{w['completion_pct']}%" if w["completion_pct"] is not None else "—"
                anch = f"{w['anchor_pct']}%" if w["anchor_pct"] is not None else "—"
                habit_summary = ", ".join(
                    f"{h}: {v['logged']}/{v['trackable']}"
                    for h, v in w.get("habits", {}).items()
                )
                lines.append(f"| {w['week_start']} | {comp} | {anch} | {w['wins']} | {habit_summary or '—'} |")

        if data["monthly"]:
            lines.append("\n### Monthly")
            lines.append("| Month | Avg Completion | Range | Avg Anchors | Wins |")
            lines.append("|-------|----------------|-------|-------------|------|")
            for m in reversed(data["monthly"]):
                comp = f"{m['completion_avg']}%" if m["completion_avg"] is not None else "—"
                rng = f"{m['completion_range'][0]}–{m['completion_range'][1]}%" if m.get("completion_range") else "—"
                anch = f"{m['anchor_avg']}%" if m["anchor_avg"] is not None else "—"
                lines.append(f"| {m['month']} | {comp} | {rng} | {anch} | {m['wins_total']} |")

        if data["quarterly"]:
            lines.append("\n### Quarterly")
            lines.append("| Quarter | Avg Completion | Range | Avg Anchors | Wins |")
            lines.append("|---------|----------------|-------|-------------|------|")
            for q in reversed(data["quarterly"]):
                comp = f"{q['completion_avg']}%" if q["completion_avg"] is not None else "—"
                rng = f"{q['completion_range'][0]}–{q['completion_range'][1]}%" if q.get("completion_range") else "—"
                anch = f"{q['anchor_avg']}%" if q["anchor_avg"] is not None else "—"
                lines.append(f"| {q['quarter']} | {comp} | {rng} | {anch} | {q['wins_total']} |")

        return "\n".join(lines)

    # --- Internal ---

    def _compute_week(self, logs) -> dict:
        """Compute stats for the current Mon–Sun week (up to today).

        Completion % and anchor % are averaged across days that had agenda data.
        Habit counts use trackable_days = non-Shabbat days in the window, because
        Shabbat is intentionally offline and should never count as a missed day.
        """
        today = date.today()
        week_start = today - timedelta(days=today.weekday())  # Monday
        days_in_week = [
            week_start + timedelta(days=i)
            for i in range(7)
            if (week_start + timedelta(days=i)) <= today
        ]

        stats = logs.compute_stats(days=7)

        comp_values, anch_values, wins_total = [], [], 0
        habit_counts: dict = defaultdict(lambda: {"logged": 0, "trackable": 0})

        for d in days_in_week:
            s = stats.get(str(d), {})
            if s.get("completion"):
                done, total = s["completion"]
                comp_values.append(round(100 * done / total))
            if s.get("anchors"):
                done, total = s["anchors"]
                anch_values.append(round(100 * done / total))
            wins_total += s.get("wins", 0)
            for habit in set(s.get("habits", [])):
                habit_counts[habit]["logged"] += 1

        # Set trackable denominator for all habits: non-Shabbat days in the window
        trackable = sum(1 for d in days_in_week if d.weekday() != 5)
        for habit in habit_counts:
            habit_counts[habit]["trackable"] = trackable

        return {
            "week_start": week_start.isoformat(),
            "completion_pct": round(sum(comp_values) / len(comp_values)) if comp_values else None,
            "anchor_pct": round(sum(anch_values) / len(anch_values)) if anch_values else None,
            "wins": wins_total,
            "habits": dict(habit_counts),
        }

    def _compress(self, data: dict) -> dict:
        """Roll old weekly entries into monthly, old monthly entries into quarterly.

        Runs on every save. Old entries are never deleted — they're aggregated, so no
        history is lost, just compressed. This is what keeps the prompt size bounded
        indefinitely regardless of how long the bot has been running.
        """
        cutoff_weekly = (date.today() - timedelta(weeks=self.WEEKLY_KEEP)).isoformat()
        cutoff_monthly = date.today().replace(day=1) - timedelta(days=self.MONTHLY_KEEP * 30)

        # Move weekly entries older than WEEKLY_KEEP weeks into monthly summaries
        old_weekly = [w for w in data["weekly"] if w["week_start"] < cutoff_weekly]
        data["weekly"] = [w for w in data["weekly"] if w["week_start"] >= cutoff_weekly]

        if old_weekly:
            by_month: dict = defaultdict(list)
            for w in old_weekly:
                by_month[w["week_start"][:7]].append(w)
            for month, weeks in by_month.items():
                if not any(m["month"] == month for m in data["monthly"]):
                    data["monthly"].append(self._aggregate_monthly(month, weeks))
            data["monthly"].sort(key=lambda m: m["month"])

        # Move monthly entries older than MONTHLY_KEEP months into quarterly summaries
        old_monthly = [m for m in data["monthly"] if m["month"] < cutoff_monthly.strftime("%Y-%m")]
        data["monthly"] = [m for m in data["monthly"] if m["month"] >= cutoff_monthly.strftime("%Y-%m")]

        if old_monthly:
            by_quarter: dict = defaultdict(list)
            for m in old_monthly:
                y, mo = m["month"].split("-")
                q = f"{y}-Q{(int(mo) - 1) // 3 + 1}"
                by_quarter[q].append(m)
            for quarter, months in by_quarter.items():
                if not any(q["quarter"] == quarter for q in data["quarterly"]):
                    data["quarterly"].append(self._aggregate_quarterly(quarter, months))
            data["quarterly"].sort(key=lambda q: q["quarter"])

        return data

    def _aggregate_monthly(self, month: str, weeks: list[dict]) -> dict:
        comp = [w["completion_pct"] for w in weeks if w["completion_pct"] is not None]
        anch = [w["anchor_pct"] for w in weeks if w["anchor_pct"] is not None]
        wins = sum(w["wins"] for w in weeks)

        habit_totals: dict = defaultdict(lambda: {"logged": 0, "trackable": 0})
        for w in weeks:
            for h, v in w.get("habits", {}).items():
                habit_totals[h]["logged"] += v["logged"]
                habit_totals[h]["trackable"] += v["trackable"]
        habit_avgs = {
            h: {
                "logged_avg": round(v["logged"] / len(weeks), 1),
                "trackable_avg": round(v["trackable"] / len(weeks), 1),
            }
            for h, v in habit_totals.items()
        }

        return {
            "month": month,
            "completion_avg": round(sum(comp) / len(comp)) if comp else None,
            "completion_range": [min(comp), max(comp)] if comp else None,
            "anchor_avg": round(sum(anch) / len(anch)) if anch else None,
            "anchor_range": [min(anch), max(anch)] if anch else None,
            "wins_total": wins,
            "habits": habit_avgs,
        }

    def _aggregate_quarterly(self, quarter: str, months: list[dict]) -> dict:
        comp = [m["completion_avg"] for m in months if m["completion_avg"] is not None]
        anch = [m["anchor_avg"] for m in months if m["anchor_avg"] is not None]
        wins = sum(m["wins_total"] for m in months)

        return {
            "quarter": quarter,
            "completion_avg": round(sum(comp) / len(comp)) if comp else None,
            "completion_range": [min(comp), max(comp)] if comp else None,
            "anchor_avg": round(sum(anch) / len(anch)) if anch else None,
            "anchor_range": [min(anch), max(anch)] if anch else None,
            "wins_total": wins,
        }

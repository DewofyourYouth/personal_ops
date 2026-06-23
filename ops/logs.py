import json
import logging
import os
import re
import html
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from db import Database

TZ = ZoneInfo("Asia/Jerusalem")

# Mirrors the format written by text_router._food_log_content, e.g.
# "lasagna — ~480 kcal, 24g protein, 21g fat, 47g carbs"
_FOOD_MACRO_RE = re.compile(
    r"~?\s*([\d.]+)\s*kcal,\s*([\d.]+)\s*g\s*protein,\s*"
    r"([\d.]+)\s*g\s*fat,\s*([\d.]+)\s*g\s*carbs",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)

# Mood: 1-5 (bad→great). Energy: 1-3 (drained→high).
# Includes legacy label/emoji fallbacks for pre-numeric data.
_MOOD_SCORES = {
    "5": 5,
    "4": 4,
    "3": 3,
    "2": 2,
    "1": 1,
    "great": 5,
    "good": 4,
    "okay": 3,
    "low": 2,
    "bad": 1,
    "😄": 5,
    "😊": 4,
    "😐": 3,
    "😕": 2,
    "😞": 1,
}
_ENERGY_SCORES = {
    "3": 3,
    "2": 2,
    "1": 1,
    "high": 3,
    "okay": 2,
    "drained": 1,
    "⚡": 3,
    "🔋": 2,
    "🪫": 1,
}


class Logs:
    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.db = Database(os.path.join(log_dir, "ops.db"))

    # --- Writing ---

    def write(
        self,
        tag: str,
        content: str,
        extra: dict | None = None,
        when: datetime | None = None,
    ):
        # `when` backdates the entry (e.g. logging yesterday's habit today). It drives the
        # ts, the day bucket, and the JSONL file alike, so the DB date (ts[:10] on replay)
        # and the recovery log stay consistent with a normal same-day write.
        now = when or datetime.now(TZ)
        ts = now.isoformat(timespec="seconds")
        date_str = (
            now.date().isoformat()
        )  # bucket by local (Jerusalem) day, matching ts
        entry = {"ts": ts, "tag": tag, "content": content, **(extra or {})}

        # Durable capture FIRST: append to JSONL before touching SQLite, so a DB
        # failure (e.g. "database is locked") can never silently lose the reading.
        # JSONL is the recovery log; sync_jsonl_to_db() can replay anything the DB missed.
        try:
            with open(self._jsonl_path(now.date()), "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("Failed to append entry to JSONL: %s", entry)

        # Primary store. Surface DB failures loudly (log + re-raise) instead of
        # dropping them; the caller can then tell the user it didn't save.
        try:
            if tag == "metric" and extra:
                self.db.insert_metric(
                    ts,
                    date_str,
                    extra.get("key", ""),
                    str(extra.get("value", "")),
                    extra.get("unit", ""),
                )
            else:
                self.db.insert_entry(ts, date_str, tag, content)
        except Exception:
            logger.exception("DB write FAILED (kept in JSONL for recovery): %s", entry)
            raise

    def write_metric(self, key: str, value, unit: str = ""):
        self.write(
            "metric",
            f"{key} {value}{unit}",
            extra={"key": key, "value": value, "unit": unit},
        )

    def sync_jsonl_to_db(self) -> int:
        """Replay JSONL entries that never made it into SQLite (e.g. a write
        dropped by a transient DB lock). JSONL is written first, so it is the
        recovery source of truth. Returns the number of rows inserted.
        """
        have_metrics = self.db.existing_metric_keys()
        have_entries = self.db.existing_entry_keys()
        inserted = 0
        for fp in sorted(Path(self.log_dir).glob("*.jsonl")):
            for line in fp.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    logger.warning("Skipping unparseable JSONL line in %s", fp.name)
                    continue
                ts, tag = e.get("ts"), e.get("tag")
                if not ts or not tag:
                    continue
                date_str = ts[:10]
                if tag == "metric" and e.get("key"):
                    k = (ts, e["key"])
                    if k in have_metrics:
                        continue
                    self.db.insert_metric(
                        ts,
                        date_str,
                        e["key"],
                        str(e.get("value", "")),
                        e.get("unit", ""),
                    )
                    have_metrics.add(k)
                    inserted += 1
                elif tag != "metric":
                    k = (ts, tag)
                    if k in have_entries:
                        continue
                    self.db.insert_entry(ts, date_str, tag, str(e.get("content", "")))
                    have_entries.add(k)
                    inserted += 1
        if inserted:
            logger.info("sync_jsonl_to_db recovered %d row(s)", inserted)
        return inserted

    # --- Reading ---

    def read_today(self) -> list[dict]:
        rows = self.db.entries_for_date(datetime.now(TZ).date())
        return [dict(r) for r in rows]

    def read_day_as_text(self, d: date) -> str:
        lines = self._read_day(d)
        return "\n".join(lines) if lines else "No log entries."

    def read_agenda_as_text(self, d: date) -> str:
        """Return the day's agenda items with their completion status, or empty string if none."""
        path = Path(self.log_dir) / f"{d}-agenda.json"
        if not path.exists():
            return ""
        try:
            items = json.loads(path.read_text()).get("items", [])
            if not items:
                return ""
            lines = []
            for item in items:
                status = item.get("status", "open")
                icon = {"done": "✅", "missed": "❌"}.get(status, "⬜")
                lines.append(f"{icon} {item['text']}")
            return "\n".join(lines)
        except Exception:
            return ""

    def read_recent(self, days: int = 3) -> str:
        sections = []
        today = datetime.now(TZ).date()
        for i in range(days, -1, -1):
            d = today - timedelta(days=i)
            lines = self._read_day(d)
            if lines:
                sections.append(f"### {d}\n" + "\n".join(lines))
        return "\n\n".join(sections) if sections else "No recent logs."

    def format_today_for_telegram(self, max_chars: int = 3900) -> list[str]:
        """Format today's human log entries as safe Telegram HTML message chunks."""
        today = datetime.now(TZ).date()
        entries = self._read_day_entries(today)
        if not entries:
            return []
        return self._format_entries_for_telegram(today, entries, max_chars=max_chars)

    # Metrics where multiple readings per day exist and the highest value is correct
    _MAX_PER_DAY_METRICS = {"steps"}

    def load_metrics(self, days: int = 14) -> dict[str, list]:
        from collections import defaultdict

        result: dict = defaultdict(list)
        start = date.today() - timedelta(days=days)

        # For most metrics: all readings
        rows = self.db.metrics_for_range(start, date.today())
        seen_max = set()
        for r in rows:
            if r["key"] in self._MAX_PER_DAY_METRICS:
                seen_max.add(r["key"])
                continue  # handled separately below
            display = f"{r['value']}{r['unit']}" if r["unit"] else r["value"]
            result[r["key"]].append((r["date"], display))

        # For max-per-day metrics: one entry per day, highest value wins
        for key in seen_max:
            max_rows = self.db.metrics_max_per_day(start, date.today(), key)
            for r in max_rows:
                display = f"{r['value']}{r['unit']}" if r["unit"] else str(r["value"])
                result[key].append((r["date"], display))
            result[key].sort(key=lambda x: x[0])

        return dict(result)

    # --- Stats ---

    _ANCHOR_KEYWORDS = {
        "meds",
        "shacharit",
        "davening",
        "chavrusa",
        "daf yomi",
        "daf",
        "yoma",
        "yerushalmi",
        "walk",
        "anki",
        "strength",
    }

    def compute_stats(self, days: int = 7) -> dict[str, dict]:
        stats = {}
        for i in range(days - 1, -1, -1):
            d = date.today() - timedelta(days=i)
            s: dict = {
                "completion": None,  # (done, total)
                "anchors": None,  # (done, total)
                "wins": 0,
                "habits": [],  # habit names logged this day
                "reminders": 0,
                "checkins": 0,
                "responded": 0,  # reminders responded to within 15 min
            }

            # Agenda completion
            agenda_path = Path(self.log_dir) / f"{d}-agenda.json"
            if agenda_path.exists():
                try:
                    items = json.loads(agenda_path.read_text()).get("items", [])
                    resolved = [i for i in items if i["status"] in ("done", "missed")]
                    if resolved:
                        done = sum(1 for i in resolved if i["status"] == "done")
                        s["completion"] = (done, len(resolved))
                    anchors = [
                        i
                        for i in items
                        if any(kw in i["text"].lower() for kw in self._ANCHOR_KEYWORDS)
                    ]
                    resolved_anchors = [
                        i for i in anchors if i["status"] in ("done", "missed")
                    ]
                    if resolved_anchors:
                        s["anchors"] = (
                            sum(1 for i in resolved_anchors if i["status"] == "done"),
                            len(resolved_anchors),
                        )
                except Exception:
                    pass

            # Log entry stats — read from SQLite, fall back to JSONL for pre-migration dates
            rows = self.db.entries_for_date(d)
            if rows:
                entries = [dict(r) for r in rows]
            else:
                entries = []
                jsonl = self._jsonl_path(d)
                if jsonl.exists():
                    for line in jsonl.read_text().splitlines():
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
            s["wins"] = sum(1 for e in entries if e.get("tag") == "win")
            s["habits"] = [
                e["content"].strip().lower() for e in entries if e.get("tag") == "habit"
            ]
            s["skips"] = [
                e["content"].strip() for e in entries if e.get("tag") == "skip"
            ]
            reminder_times = [
                datetime.fromisoformat(e["ts"])
                for e in entries
                if e.get("tag") == "reminder"
            ]
            checkin_times = [
                datetime.fromisoformat(e["ts"])
                for e in entries
                if e.get("tag") == "checkin"
            ]
            s["reminders"] = len(reminder_times)
            s["checkins"] = len(checkin_times)
            if reminder_times:
                s["responded"] = sum(
                    1
                    for rt in reminder_times
                    if any(
                        timedelta(0) <= ct - rt <= timedelta(minutes=15)
                        for ct in checkin_times
                    )
                )

            stats[str(d)] = s
        return stats

    def format_stats_for_prompt(self, days: int = 7) -> str:
        stats = self.compute_stats(days=days)
        days_with_data = [s for s in stats.values() if s["completion"] or s["wins"]]
        if not days_with_data:
            return ""

        lines = ["## Daily stats\n"]
        lines.append("| Date | Completion | Anchors | Wins |")
        lines.append("|------|------------|---------|------|")

        for date_str, s in stats.items():
            comp = (
                f"{s['completion'][0]}/{s['completion'][1]} ({100 * s['completion'][0] // s['completion'][1]}%)"
                if s["completion"]
                else "—"
            )
            anch = (
                f"{s['anchors'][0]}/{s['anchors'][1]} ({100 * s['anchors'][0] // s['anchors'][1]}%)"
                if s["anchors"]
                else "—"
            )
            wins = str(s["wins"]) if s["wins"] else "—"
            skip_note = (
                f" ⚠️ skip: {'; '.join(s.get('skips', []))}" if s.get("skips") else ""
            )
            lines.append(f"| {date_str} | {comp} | {anch} | {wins} |{skip_note}")

        # Rolling averages over days with completion data
        comp_days = [s["completion"] for s in stats.values() if s["completion"]]
        anch_days = [s["anchors"] for s in stats.values() if s["anchors"]]
        total_wins = sum(s["wins"] for s in stats.values())
        summary = []
        if comp_days:
            avg = sum(d / t for d, t in comp_days) / len(comp_days)
            summary.append(f"avg completion {avg:.0%}")
        if anch_days:
            avg = sum(d / t for d, t in anch_days) / len(anch_days)
            summary.append(f"avg anchor rate {avg:.0%}")
        if total_wins:
            summary.append(f"{total_wins} wins logged")
        if summary:
            lines.append(
                "\n**Rolling (" + str(days) + " days):** " + ", ".join(summary)
            )

        # Habit frequency table
        from collections import Counter

        habit_counts: Counter = Counter()
        days_with_habit_logging = sum(1 for s in stats.values() if s["habits"])
        for s in stats.values():
            habit_counts.update(set(s["habits"]))  # count days, not occurrences
        if habit_counts:
            earliest_habit = self.earliest_habit_date()
            habit_days = (
                (date.today() - earliest_habit).days + 1 if earliest_habit else days
            )
            habit_window = min(days, habit_days)
            # Shabbat (Saturday) is explicitly excluded from habit tracking
            shabbat_in_window = sum(
                1
                for i in range(habit_window)
                if (date.today() - timedelta(days=i)).weekday() == 5
            )
            trackable_days = habit_window - shabbat_in_window
            lines.append("\n## Habit log\n")
            lines.append(
                f"_(logged via `habit:` prefix; {days_with_habit_logging}/{trackable_days} non-Shabbat days had any habit entries"
                + (
                    f" — habit tracking started {earliest_habit})"
                    if earliest_habit and habit_days < days
                    else ")"
                )
                + "_\n"
            )
            lines.append("| Habit | Days logged |")
            lines.append("|-------|-------------|")
            for habit, count in sorted(habit_counts.items(), key=lambda x: -x[1]):
                lines.append(f"| {habit} | {count}/{trackable_days} |")

        return "\n".join(lines)

    def format_metrics_for_prompt(self, days: int = 14) -> str:
        """Pre-compute stats for health metrics; show recent entries for others.

        Health metrics (steps, weight) get 7-day and 30-day averages plus trend
        direction — the LLM gets numbers, not raw data to average itself.
        Other metrics (mood, energy, custom) show the last 7 readings as-is.
        """
        lines = []

        # --- Health metrics: pre-computed summaries ---
        steps_summary = self._steps_summary()
        if steps_summary:
            lines.append(f"Steps: {steps_summary}")

        weight_summary = self._weight_summary()
        if weight_summary:
            lines.append(f"Weight: {weight_summary}")

        # --- Other metrics: recent entries ---
        data = self.load_metrics(days=days)
        other_keys = [k for k in sorted(data.keys()) if k not in ("steps", "weight")]
        for key in other_keys:
            recent = ", ".join(f"{d}: {v}" for d, v in data[key][-7:])
            lines.append(f"  {key}: {recent}")

        return ("Tracked metrics:\n" + "\n".join(lines)) if lines else ""

    def _steps_summary(self) -> str:
        today = date.today()
        start_7 = today - timedelta(days=6)
        start_30 = today - timedelta(days=29)

        def _active_avg(rows) -> float | None:
            vals = []
            for r in rows:
                try:
                    d = date.fromisoformat(r["date"])
                    if d.weekday() not in (4, 5):  # exclude Fri/Sat
                        vals.append(float(r["value"]))
                except (ValueError, TypeError):
                    pass
            return round(sum(vals) / len(vals)) if vals else None

        rows_7 = self.db.metrics_max_per_day(start_7, today, "steps")
        rows_30 = self.db.metrics_max_per_day(start_30, today, "steps")

        avg_7 = _active_avg(rows_7)
        avg_30 = _active_avg(rows_30)

        if avg_7 is None:
            return ""

        recent = ", ".join(
            f"{r['date']}: {r['value']}"
            for r in rows_7
            if date.fromisoformat(r["date"]).weekday() not in (4, 5)
        )

        trend = ""
        if avg_7 and avg_30:
            diff = avg_7 - avg_30
            if diff > 300:
                trend = " ↑ vs 30-day baseline"
            elif diff < -300:
                trend = " ↓ vs 30-day baseline"
            else:
                trend = " → flat vs 30-day baseline"

        parts = []
        if recent:
            parts.append(f"last 7 days (excl. Fri/Sat): {recent}")
        if avg_7:
            parts.append(f"7-day avg: {avg_7:,}")
        if avg_30:
            parts.append(f"30-day avg: {avg_30:,}{trend}")
        return " | ".join(parts)

    def _weight_summary(self) -> str:
        today = date.today()
        start_7 = today - timedelta(days=6)
        start_30 = today - timedelta(days=29)

        def _latest_per_day(rows) -> list[tuple[str, float]]:
            result = []
            for r in rows:
                try:
                    result.append((r["date"], float(r["value"])))
                except (ValueError, TypeError):
                    pass
            return result

        rows_7 = self.db.metrics_max_per_day(start_7, today, "weight")
        rows_30 = self.db.metrics_max_per_day(start_30, today, "weight")

        entries_7 = _latest_per_day(rows_7)
        entries_30 = _latest_per_day(rows_30)

        if not entries_7:
            return ""

        avg_7 = (
            round(sum(v for _, v in entries_7) / len(entries_7), 1)
            if entries_7
            else None
        )
        avg_30 = (
            round(sum(v for _, v in entries_30) / len(entries_30), 1)
            if entries_30
            else None
        )

        recent_vals = " → ".join(f"{v}" for _, v in entries_7[-5:])

        trend = ""
        if avg_7 and avg_30:
            diff = avg_7 - avg_30
            if diff < -0.3:
                trend = f" ↓ vs 30-day avg ({avg_30} kg)"
            elif diff > 0.3:
                trend = f" ↑ vs 30-day avg ({avg_30} kg)"
            else:
                trend = f" → flat vs 30-day avg ({avg_30} kg)"

        parts = [f"recent: {recent_vals} kg"]
        if avg_7:
            parts.append(f"7-day avg: {avg_7} kg{trend}")
        return " | ".join(parts)

    # --- Food macro persistence ---

    def save_food_summary(self, d: date) -> dict | None:
        """Compute and persist the day's macro totals to the food_summary table.

        Returns the saved totals dict, or None if there were no estimable entries.
        Idempotent — safe to call multiple times for the same day (upserts).
        """
        rows = self.db.entries_for_date(d)
        food_contents = [r["content"] for r in rows if r["tag"] == "food"]
        if not food_contents:
            return None
        parsed = []
        for content in food_contents:
            m = _FOOD_MACRO_RE.search(content)
            if m:
                kcal, protein, fat, carbs = (float(g) for g in m.groups())
                parsed.append(
                    {"kcal": kcal, "protein_g": protein, "fat_g": fat, "carbs_g": carbs}
                )
        if not parsed:
            return None
        totals = {
            k: sum(p[k] for p in parsed)
            for k in ("kcal", "protein_g", "fat_g", "carbs_g")
        }
        self.db.upsert_food_summary(
            d.isoformat(),
            totals["kcal"],
            totals["protein_g"],
            totals["fat_g"],
            totals["carbs_g"],
            len(food_contents),
        )
        return totals

    def format_food_for_prompt(
        self, days: int = 14, end_date: date | None = None
    ) -> str:
        """Recent daily macro totals as a formatted block for LLM prompts."""
        end = end_date or date.today()
        start = end - timedelta(days=days - 1)
        rows = self.db.food_summary_for_range(start, end)
        if not rows:
            return ""

        def _fmt(n: float) -> str:
            return str(int(n)) if float(n).is_integer() else str(round(n, 1))

        lines = ["## Food / macros\n"]
        lines.append("| Date | kcal | protein | fat | carbs | entries |")
        lines.append("|------|------|---------|-----|-------|---------|")
        kcal_vals = []
        for r in rows:
            lines.append(
                f"| {r['date']} | ~{_fmt(r['kcal'])} | {_fmt(r['protein_g'])}g | "
                f"{_fmt(r['fat_g'])}g | {_fmt(r['carbs_g'])}g | {r['entry_count']} |"
            )
            kcal_vals.append(r["kcal"])
        if len(kcal_vals) >= 3:
            avg_kcal = round(sum(kcal_vals) / len(kcal_vals))
            lines.append(f"\n**Avg (logged days only):** ~{avg_kcal:,} kcal/day")
        return "\n".join(lines)

    def earliest_log_date(self) -> date | None:
        return self.db.earliest_entry_date()

    def earliest_habit_date(self) -> date | None:
        return self.db.earliest_entry_date_with_tag("habit")

    @staticmethod
    def _mood_energy_score(key: str, val: str):
        """Normalise a mood/energy value (numeric, label, or emoji) to its score, or None."""
        if key == "mood":
            return _MOOD_SCORES.get(val)
        if key == "energy":
            return _ENERGY_SCORES.get(val)
        return None

    def _mood_energy_readings(
        self, start: date, end: date
    ) -> list[tuple[datetime, str, int]]:
        """Timestamped mood/energy readings across [start, end] as (ts, key, score).

        Reads from SQLite, falling back to JSONL for any day with no DB metrics
        (pre-migration data). Legacy label/emoji values are normalised to numbers.
        """
        readings: list[tuple[datetime, str, int]] = []
        db_dates = set()
        for r in self.db.metrics_for_range(start, end):
            db_dates.add(r["date"])
            score = self._mood_energy_score(r["key"], str(r["value"]))
            if score is not None:
                readings.append((datetime.fromisoformat(r["ts"]), r["key"], score))

        span = (end - start).days
        for i in range(span + 1):
            d = start + timedelta(days=i)
            if d.isoformat() in db_dates:
                continue
            path = self._jsonl_path(d)
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                try:
                    e = json.loads(line)
                    if e.get("tag") != "metric":
                        continue
                    score = self._mood_energy_score(
                        e.get("key", ""), str(e.get("value", ""))
                    )
                    if score is not None:
                        readings.append(
                            (datetime.fromisoformat(e["ts"]), e["key"], score)
                        )
                except Exception:
                    pass

        return readings

    def mood_energy_for_range(
        self, start: date, end: date
    ) -> tuple[list[int], list[int]]:
        """Collect numeric mood (1-5) and energy (1-3) readings across [start, end]."""
        moods, energies = [], []
        for _, key, score in self._mood_energy_readings(start, end):
            (moods if key == "mood" else energies).append(score)
        return moods, energies

    # Time-of-day buckets for diurnal mood/energy analysis. (label, start_hour, end_hour);
    # end is exclusive. Covers the full 24h; "late" catches the 3am can't-sleep entries.
    _TOD_BUCKETS = (
        ("late night", 0, 5),
        ("morning", 5, 12),
        ("afternoon", 12, 18),
        ("evening", 18, 24),
    )

    def mood_energy_by_time_of_day(self, days: int = 14) -> dict:
        """Average mood/energy split by time of day over the last `days`.

        Pure analysis of the existing timestamped readings — answers "am I happier in
        the morning or the afternoon" without any new logging. Returns a dict keyed by
        bucket label; only buckets with data are included.
        """
        start = date.today() - timedelta(days=days)
        acc: dict[str, dict[str, list]] = {
            label: {"mood": [], "energy": []} for label, _, _ in self._TOD_BUCKETS
        }
        for ts, key, score in self._mood_energy_readings(start, date.today()):
            for label, lo, hi in self._TOD_BUCKETS:
                if lo <= ts.hour < hi:
                    acc[label][key].append(score)
                    break

        result = {}
        for label, _, _ in self._TOD_BUCKETS:
            moods, energies = acc[label]["mood"], acc[label]["energy"]
            if not moods and not energies:
                continue
            result[label] = {
                "mood_avg": round(sum(moods) / len(moods), 1) if moods else None,
                "energy_avg": round(sum(energies) / len(energies), 1)
                if energies
                else None,
                "n": len(moods) + len(energies),
            }
        return result

    def format_time_of_day_for_prompt(self, days: int = 14) -> str:
        """Render the diurnal mood/energy breakdown for an LLM prompt. Empty if no data."""
        tod = self.mood_energy_by_time_of_day(days=days)
        if not tod:
            return ""
        lines = [
            f"Mood/energy by time of day (last {days} days; "
            "mood 1-5, energy 1-3, n = total readings):"
        ]
        for label, lo, hi in self._TOD_BUCKETS:
            if label not in tod:
                continue
            b = tod[label]
            mood = b["mood_avg"] if b["mood_avg"] is not None else "—"
            energy = b["energy_avg"] if b["energy_avg"] is not None else "—"
            lines.append(
                f"  {label.capitalize()} ({lo:02d}:00-{hi:02d}:00): "
                f"mood {mood}, energy {energy} (n={b['n']})"
            )
        return "\n".join(lines)

    def read_day_difficulty(self, d: date) -> str:
        """Return 'hard', 'okay', or 'good' based on mood/energy logged for the day."""
        moods, energies = self.mood_energy_for_range(d, d)
        if not moods and not energies:
            return "okay"

        # Mood: 1-5, Energy: 1-3. Hard = drained energy or low/bad mood pulling avg down.
        has_drained = any(e == 1 for e in energies)
        has_bad_mood = any(m <= 2 for m in moods)
        mood_avg = sum(moods) / len(moods) if moods else 3
        energy_avg = sum(energies) / len(energies) if energies else 2

        if has_drained or (has_bad_mood and mood_avg < 2.5):
            return "hard"
        if mood_avg >= 4 and energy_avg >= 2.5:
            return "good"
        return "okay"

    # --- Internal ---

    def _jsonl_path(self, d: date) -> Path:
        return Path(self.log_dir) / f"{d}.jsonl"

    # Tags excluded from the human/LLM-facing day read: metrics render via their own
    # summaries, and `reminder` entries are bot-fired prompt noise (legacy auto-logged
    # check-in nudges) that should never count as activity context.
    _UNREADABLE_TAGS = {"metric", "reminder"}

    def _read_day_entries(self, d: date) -> list[dict]:
        """Structured entries for a day, using SQLite first and JSONL as fallback."""
        rows = self.db.entries_for_date(d)
        if rows:
            return [dict(r) for r in rows if r["tag"] not in self._UNREADABLE_TAGS]

        jsonl = self._jsonl_path(d)
        if not jsonl.exists():
            return []

        entries = []
        for line in jsonl.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("tag") in self._UNREADABLE_TAGS:
                continue
            entries.append(
                {
                    "ts": str(e.get("ts", "")),
                    "tag": str(e.get("tag", "")),
                    "content": str(e.get("content", "")),
                }
            )
        return entries

    @staticmethod
    def _entry_html(entry: dict, max_chars: int | None = None) -> str:
        ts = str(entry.get("ts", ""))
        t = ts[11:16] if len(ts) >= 16 else "??:??"
        tag = str(entry.get("tag", ""))
        is_discrete = tag == "discrete"
        prefix = f"<code>{t}</code> <b>#{html.escape(tag)}</b> "
        content = str(entry.get("content", ""))

        def _render(text: str) -> str:
            escaped = html.escape(text)
            return f"<tg-spoiler>{escaped}</tg-spoiler>" if is_discrete else escaped

        if max_chars is None:
            return prefix + _render(content)

        suffix = " …(entry truncated)"
        if len(prefix + html.escape(content)) <= max_chars:
            return prefix + _render(content)
        if max_chars <= len(prefix) + len(suffix):
            return (prefix + suffix).strip()[:max_chars]

        lo, hi = 0, len(content)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            candidate = prefix + html.escape(content[:mid]) + suffix
            if len(candidate) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        return prefix + _render(content[:lo]) + suffix

    def _format_entries_for_telegram(
        self, d: date, entries: list[dict], max_chars: int = 3900
    ) -> list[str]:
        header = f"📋 <b>Log for {d} ({len(entries)} entries):</b>\n\n"
        chunks: list[str] = []
        current = header

        for entry in entries:
            line = self._entry_html(entry)
            sep = "" if current.endswith("\n\n") else "\n"
            if len(current) + len(sep) + len(line) <= max_chars:
                current += sep + line
                continue

            if current:
                chunks.append(current.rstrip())
                current = ""

            if len(line) <= max_chars:
                current = line
                continue

            chunks.append(self._entry_html(entry, max_chars=max_chars))
            current = ""

        if current:
            chunks.append(current.rstrip())
        return chunks

    def _read_day(self, d: date) -> list[str]:
        # Read from SQLite (primary). Fall back to JSONL then MD for pre-migration dates.
        rows = self.db.entries_for_date(d)
        if rows:
            return [
                f"[{r['ts']}] #{r['tag']}: {r['content']}"
                for r in rows
                if r["tag"] not in self._UNREADABLE_TAGS
            ]
        # Fallback: JSONL (pre-migration data)
        jsonl = self._jsonl_path(d)
        md = Path(self.log_dir) / f"{d}.md"
        if jsonl.exists():
            lines = []
            for line in jsonl.read_text().splitlines():
                try:
                    e = json.loads(line)
                    if e.get("tag") not in self._UNREADABLE_TAGS:
                        lines.append(f"[{e['ts']}] #{e['tag']}: {e['content']}")
                except Exception:
                    pass
            return lines
        if md.exists():
            return self._parse_md(md.read_text())
        return []

    @staticmethod
    def _parse_md(text: str) -> list[str]:
        lines = []
        tag = content = None
        for line in text.splitlines():
            m = re.match(r"^## (\d{2}:\d{2}) (#\w+)$", line)
            if m:
                if tag and content:
                    lines.append(f"[{m.group(1)}] {tag}: {content}")
                tag, content = m.group(2), ""
            elif tag is not None:
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith("- [ ]")
                    and stripped != "## Agenda"
                ):
                    content = (content + " " + stripped).strip()
        if tag and content:
            lines.append(f"[?] {tag}: {content}")
        return lines

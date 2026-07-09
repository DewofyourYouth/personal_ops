"""Mine the logs for quantitative, actionable patterns — the complement to the qualitative
insight ledger (insights.py). Deterministic core: it computes per-day series (mood, energy,
steps, weight, food kcal, habits, free-text themes) and reports correlations, weekday
effects, and habit→mood associations. An optional --advise pass hands the computed numbers
(never the raw journal) to the LLM for synthesis.

Everything here is descriptive over a small sample (~6 weeks); effect sizes are reported
with n so nothing reads as more certain than it is. Correlation ≠ cause — these are leads.

    venv/bin/python ops/mine_logs.py            # the numbers
    venv/bin/python ops/mine_logs.py --advise   # + LLM synthesis from the numbers
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from datetime import datetime

import numpy as np

DB_PATH = "ops/log/ops.db"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MOOD_EMOJI = {"😄": 5, "😊": 4, "😐": 3, "😕": 2, "😞": 1}
ENERGY_EMOJI = {"⚡": 3, "🔋": 2, "🪫": 1}

# Recurring themes seen in the free text — (label, regex over content).
THEMES = {
    "job search / money": r"\b(job|interview|unemploy|rejected|headhunter|linkedin|applied|recruiter|benefits ran out|hired|salary)\b",
    "chavrusa flakiness": r"\b(chavrusa|khavrusa|chavusa|chavruta)\b.{0,40}\b(sick|flak|resched|cancel|tapped|late|no[- ]?show|unavailable)\b",
    "sleep trouble": r"\b(couldn'?t sleep|didn'?t sleep|insomnia|slept (badly|late)|sleeping pill|up (all|half) the night|2 ?a\.?m|3 ?a\.?m|overslept|nap)\b",
    "kids overwhelm": r"\b(kids?|three[- ]year|3[- ]year|toddler|meltdown|screaming|tantrum)\b",
    "missed davening": r"\b(missed|late for|skipped).{0,20}\b(shacharit|shachris|shacharis|davening|minyan|shul|maariv|mincha)\b",
    "a walk": r"\b(walk|stroll)\b",
}


def _num(v: str) -> float | None:
    m = re.match(r"^-?\d+(\.\d+)?", v.strip())
    return float(m.group(0)) if m else None


def load_days(c) -> dict[str, dict]:
    """One record per calendar day with the numeric series and same-day text/habits."""
    days: dict[str, dict] = defaultdict(
        lambda: {
            "mood": [],
            "energy": [],
            "steps": None,
            "weight": None,
            "kcal": None,
            "sleep": None,
            "habits": set(),
            "missed": set(),
            "text": [],
        }
    )
    for key, val, d in c.execute("SELECT key, value, date FROM metrics"):
        rec = days[d]
        if key == "mood":
            n = MOOD_EMOJI.get(val, _num(val))
            if n:
                rec["mood"].append(n)
        elif key == "energy":
            n = ENERGY_EMOJI.get(val, _num(val))
            if n:
                rec["energy"].append(n)
        elif key == "steps" and (n := _num(val)):
            rec["steps"] = max(rec["steps"] or 0, n)
        elif key == "weight" and (n := _num(val)):
            rec["weight"] = n
        elif key == "sleep" and (n := _num(val)):
            rec["sleep"] = n
    for d, kcal in c.execute("SELECT date, kcal FROM food_summary"):
        days[d]["kcal"] = kcal
    for tag, content, d in c.execute("SELECT tag, content, date FROM entries"):
        rec = days[d]
        if tag == "habit":
            rec["habits"].add(content.strip())
        elif tag == "habit_missed":
            rec["missed"].add(content.strip())
        if tag in ("checkin", "log", "wrong", "win", "insight", "note"):
            rec["text"].append(content.lower())
    # collapse mood/energy lists to daily means
    for rec in days.values():
        rec["mood"] = float(np.mean(rec["mood"])) if rec["mood"] else None
        rec["energy"] = float(np.mean(rec["energy"])) if rec["energy"] else None
        rec["text"] = " \n ".join(rec["text"])
    return dict(days)


def _pairs(days, xk, yk):
    xs, ys = [], []
    for rec in days.values():
        if rec[xk] is not None and rec[yk] is not None:
            xs.append(rec[xk])
            ys.append(rec[yk])
    return np.array(xs), np.array(ys)


def _corr(days, xk, yk) -> tuple[float, int]:
    xs, ys = _pairs(days, xk, yk)
    if len(xs) < 5 or xs.std() == 0 or ys.std() == 0:
        return float("nan"), len(xs)
    return float(np.corrcoef(xs, ys)[0, 1]), len(xs)


def _mean_sd(vals):
    vals = [v for v in vals if v is not None]
    return (float(np.mean(vals)), len(vals)) if vals else (float("nan"), 0)


def report(days: dict) -> str:
    out: list[str] = []
    p = out.append
    dated = {d: r for d, r in days.items() if re.match(r"\d{4}-\d{2}-\d{2}", d)}

    p("═══ LOG MINING REPORT ═══")
    p(f"Days with any data: {len(dated)}  ({min(dated)} → {max(dated)})\n")

    # 1. Weekday effect on mood/energy — tests the "Friday stress" hypothesis
    p("① Mood & energy by weekday (Friday-anxiety check):")
    by_wd_m: dict[int, list] = defaultdict(list)
    by_wd_e: dict[int, list] = defaultdict(list)
    for d, r in dated.items():
        wd = datetime.strptime(d, "%Y-%m-%d").weekday()
        if r["mood"] is not None:
            by_wd_m[wd].append(r["mood"])
        if r["energy"] is not None:
            by_wd_e[wd].append(r["energy"])
    for wd in range(7):
        m, mn = _mean_sd(by_wd_m[wd])
        e, en = _mean_sd(by_wd_e[wd])
        bar = "█" * round((m if m == m else 0) * 2)
        p(
            f"   {WEEKDAYS[wd]}  mood {m:4.1f} (n={mn:2})  energy {e:4.1f} (n={en:2})  {bar}"
        )
    p("")

    # 2. Numeric correlations
    p("② Correlations (per-day, r with n):")
    for xk, yk, label in [
        ("sleep", "mood", "sleep hrs ↔ mood"),
        ("sleep", "energy", "sleep hrs ↔ energy"),
        ("steps", "mood", "steps ↔ mood"),
        ("steps", "energy", "steps ↔ energy"),
        ("kcal", "energy", "food kcal ↔ energy"),
        ("kcal", "mood", "food kcal ↔ mood"),
        ("mood", "energy", "mood ↔ energy"),
        ("weight", "mood", "weight ↔ mood"),
    ]:
        r, n = _corr(dated, xk, yk)
        flag = "  ←notable" if r == r and abs(r) >= 0.3 and n >= 8 else ""
        p(f"   {label:22} r={r:+.2f}  (n={n}){flag}")
    p("")

    # 3. Habit → next-mood association: mood on days a habit was done vs not
    p("③ Habit → same-day mood (done vs not-done days, min 4 done-days):")
    all_habits = set()
    for r in dated.values():
        all_habits |= r["habits"]
    rows = []
    for h in all_habits:
        done = [
            r["mood"]
            for r in dated.values()
            if h in r["habits"] and r["mood"] is not None
        ]
        notd = [
            r["mood"]
            for r in dated.values()
            if h not in r["habits"] and r["mood"] is not None
        ]
        if len(done) >= 4 and notd:
            rows.append((np.mean(done) - np.mean(notd), h, np.mean(done), len(done)))
    for delta, h, dm, n in sorted(rows, reverse=True):
        p(f"   {h[:34]:34} mood {dm:4.1f} on done-days  Δ{delta:+.1f}  (n={n})")
    p("")

    # 4. Theme frequency + mood on theme-days
    p("④ Recurring themes (days mentioned; mood on those days vs overall):")
    overall_mood = _mean_sd([r["mood"] for r in dated.values()])[0]
    for label, pat in THEMES.items():
        rx = re.compile(pat, re.IGNORECASE)
        hit = [r for r in dated.values() if rx.search(r["text"])]
        moods = [r["mood"] for r in hit if r["mood"] is not None]
        mm, mn = _mean_sd(moods)
        delta = (mm - overall_mood) if mm == mm else float("nan")
        p(
            f"   {label:22} {len(hit):2} days   mood {mm:4.1f} (n={mn:2})  Δ{delta:+.1f} vs {overall_mood:.1f}"
        )
    p("")

    # 5. Weight trajectory
    wser = sorted((d, r["weight"]) for d, r in dated.items() if r["weight"] is not None)
    if len(wser) >= 2:
        d0, w0 = wser[0]
        d1, w1 = wser[-1]
        span = (
            datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d0, "%Y-%m-%d")
        ).days or 1
        rate = (w1 - w0) / span * 7
        p("⑤ Weight trajectory:")
        p(
            f"   {w0:.1f} → {w1:.1f} kg over {span} days  ({rate:+.2f} kg/week, n={len(wser)})"
        )
        p("")

    # 6. Habit adherence (done vs missed logs)
    p("⑥ Habit adherence (logged done vs missed):")
    done_c = defaultdict(int)
    miss_c = defaultdict(int)
    for r in dated.values():
        for h in r["habits"]:
            done_c[h] += 1
        for h in r["missed"]:
            miss_c[h] += 1
    for h in sorted(set(done_c) | set(miss_c), key=lambda h: -(done_c[h] + miss_c[h]))[
        :12
    ]:
        tot = done_c[h] + miss_c[h]
        rate = done_c[h] / tot if tot else 0
        p(f"   {h[:34]:34} {done_c[h]:2}✓ {miss_c[h]:2}✗  ({rate:.0%} done)")
    return "\n".join(out)


def report_for(db_path: str = DB_PATH) -> str:
    """Build the deterministic mining report for a given DB — used by the /mine command
    and the weekly job so they share the CLI's exact logic."""
    return report(load_days(sqlite3.connect(db_path)))


async def advise(report_text: str) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[
            {
                "role": "user",
                "content": (
                    "These are computed statistics from my personal-ops logs (~6 weeks). "
                    "Give me 4–6 specific, actionable pieces of advice grounded ONLY in these "
                    "numbers. Name the stat behind each. Flag where n is too small to trust. "
                    "Be direct, not therapeutic; no moralizing.\n\n" + report_text
                ),
            }
        ],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--advise", action="store_true", help="add an LLM synthesis of the numbers"
    )
    args = ap.parse_args()
    c = sqlite3.connect(DB_PATH)
    days = load_days(c)
    text = report(days)
    print(text)
    if args.advise:
        print("\n═══ SYNTHESIS ═══")
        print(await advise(text))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

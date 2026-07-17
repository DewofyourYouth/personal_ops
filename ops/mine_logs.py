"""Mine the logs for quantitative, actionable patterns — the complement to the qualitative
insight ledger (insights.py). Deterministic core: it computes per-day series (mood, energy,
steps, weight, food kcal, habits, free-text themes) and reports correlations, weekday
effects, and habit→mood associations.

The printed report (report()/report_for()) is for human eyes — every row shows its n so
you can weigh it yourself. The --advise pass is a SEPARATE, stricter pipeline: build_findings()
gates each candidate finding (sample-size floor + a 95% CI that excludes zero) into a typed
Finding with a verdict ("report"/"suppress"/"alert") BEFORE anything reaches the LLM, and
tags each with what it can support in prose (same-day correlation vs. lagged vs. trend vs.
purely descriptive). The LLM only ever sees verdict=="report" findings, must cite a finding
id on every claim, and any citation that doesn't resolve gets dropped post-hoc. This exists
because caveating the LLM's output after generation doesn't work — the model had already
picked its narrative by then; the fix is to not let it see rows it shouldn't act on.

Everything here is descriptive over a small sample (~6 weeks); effect sizes are reported
with n so nothing reads as more certain than it is. Correlation ≠ cause — these are leads.

    venv/bin/python ops/mine_logs.py            # the numbers
    venv/bin/python ops/mine_logs.py --advise   # + LLM synthesis from the numbers
    venv/bin/python ops/mine_logs.py --affect   # voice-note affect_features vs self_mood_rating
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from tags import TEXT_MINING_TAGS

DB_PATH = "ops/log/ops.db"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Below this, a row in a ranked list (habits, themes) is noise dressed as a
# finding rather than a lead. Fixed named-hypothesis sections (② correlations,
# ④ lagged mood→habit, the affect report) keep the row either way — the reader
# needs to see "not enough data" rather than have the row silently vanish —
# but only flag it "notable" once n clears this floor too.
MIN_N = 10

# Voice-note prosodic features that ride in entries.extra (see affect.py).
AFFECT_FEATURES = [
    "pitch_var",
    "speech_rate",
    "pause_ms",
    "pause_count",
    "energy",
    "duration_s",
]
# A mood tap more than this many minutes from the voice note isn't "the same
# moment" anymore — pairing it in would just add noise, not signal.
AFFECT_MAX_GAP_MIN = 30

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
        if tag in TEXT_MINING_TAGS:
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


def _dated_only(days: dict) -> dict:
    return {d: r for d, r in days.items() if re.match(r"\d{4}-\d{2}-\d{2}", d)}


def _corr_ci95(r: float, n: int) -> tuple[float, float] | None:
    """95% CI on a Pearson r via the Fisher z-transform. None when n is too
    small for the transform (n<=3) or r is exactly +-1 (z undefined)."""
    if r != r or n <= 3 or abs(r) >= 1:
        return None
    z = np.arctanh(r)
    se = 1 / np.sqrt(n - 3)
    return float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se))


def _mean_diff_ci95(a, b) -> tuple[float, float] | None:
    """95% CI on mean(a) - mean(b), normal-approximation (unequal n/variance
    — habit done-days vs not-done-days are never balanced). Good enough at
    this sample size; not a full Welch-t, same idea."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 2 or len(b) < 2:
        return None
    diff = float(a.mean() - b.mean())
    se = float(np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))
    if se == 0:
        # Zero variance in both groups — the CI is degenerate (a point), not
        # unknowable. (diff, diff) still correctly "excludes zero" iff the
        # groups actually differ.
        return diff, diff
    return diff - 1.96 * se, diff + 1.96 * se


def _ci_excludes_zero(ci: tuple[float, float] | None) -> bool:
    return ci is not None and (ci[0] > 0 or ci[1] < 0)


# What a finding's causal footing licenses in prose, passed to the LLM prompt
# verbatim per tag actually present. Keeps the generator from writing "do X to
# get Y" over a stat that only ever showed X and Y moving together.
TAG_GUIDANCE = {
    "correlational_same_session": (
        "same-day correlation only. Never phrase as predictive or causal — "
        "no 'X predicts Y', no 'watch X to anticipate Y', no 'do X to get Y'."
    ),
    "same_day_confounded": (
        "a same-day association where either direction (or a shared third "
        "cause) is equally plausible. Never phrase as 'do X to get Y' or "
        "'X causes/improves/produces Y' — describe it as co-occurrence only."
    ),
    "lagged_leading_indicator": (
        "yesterday's value vs today's outcome — temporal order rules out "
        "reverse causation but not a shared confound. May be phrased as an "
        "early-warning signal, never as a lever ('doing X will cause Y')."
    ),
    "longitudinal_trend": (
        "a measured trend over the full tracked period — may be described "
        "as a trajectory or rate, not attributed to a single cause unless "
        "that cause is stated in the data itself."
    ),
    "descriptive": (
        "a plain count or rate, not a comparison — report the number, draw "
        "no correlational or causal conclusion from it."
    ),
}

# Stricter than the printed report's MIN_N: this gates what reaches the LLM,
# where a marginal row reads as sanctioned advice rather than a number the
# reader can weigh themselves.
MIN_N_ADVICE = 15


@dataclass(frozen=True)
class Finding:
    """One candidate finding, gated BEFORE it can reach a prompt — the stats
    layer's output. `verdict` decides what a prose generator is even allowed
    to see: "report" (cleared the n floor and a 95% CI that excludes zero),
    "suppress" (not enough evidence — just insufficient, not broken), or
    "alert" (zero data for a tracked metric — a logging gap, not a null
    result, and never something a generator should narrate as a finding)."""

    id: str
    label: str
    stat: str
    n: int
    tag: str = ""
    verdict: str = "report"
    note: str = ""


def report(days: dict) -> str:
    out: list[str] = []
    p = out.append
    dated = _dated_only(days)

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

    # 2. Numeric correlations. A zero/nan row isn't "no correlation" — it's no
    # data, which is a different claim — so those are pulled into a separate
    # data-gap note instead of sitting inline looking like a null result.
    p("② Correlations (per-day, r with n):")
    gaps = []
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
        if n == 0 or r != r:
            gaps.append(label)
            continue
        flag = "  ←notable" if abs(r) >= 0.3 and n >= MIN_N else ""
        p(f"   {label:22} r={r:+.2f}  (n={n}){flag}")
    if gaps:
        p(f"   ⚠ no data yet: {', '.join(gaps)}")
    p("")

    # 3. Habit ↔ same-day mood: same-day correlation only, direction unknown —
    # this alone can't distinguish "habit lifts mood" from "low mood suppresses
    # habit-doing". See ④ for the lagged check that tests direction.
    p(
        f"③ Habit ↔ same-day mood (association only, not direction; min {MIN_N} done-days):"
    )
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
        if len(done) >= MIN_N and notd:
            rows.append((np.mean(done) - np.mean(notd), h, np.mean(done), len(done)))
    for delta, h, dm, n in sorted(rows, reverse=True):
        p(f"   {h[:34]:34} mood {dm:4.1f} on done-days  Δ{delta:+.1f}  (n={n})")
    p("")

    # 4. Lagged mood → next-day habit count: does yesterday's mood predict how
    # many habits get done today? If ③'s association were "habit lifts mood",
    # this lagged direction should be weak/absent; if it's "low mood suppresses
    # habits", a same-day effect is still possible without this lag showing up
    # (mood can depress habits the same day it's felt, not just the day after).
    p("④ Prior-day mood → today's habit count (lagged, tests ③'s direction):")
    lag_x, lag_y = [], []
    for d in sorted(dated):
        prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        if prev in dated and dated[prev]["mood"] is not None:
            lag_x.append(dated[prev]["mood"])
            lag_y.append(len(dated[d]["habits"]))
    lx, ly = np.array(lag_x), np.array(lag_y)
    if len(lx) >= 5 and lx.std() and ly.std():
        r = float(np.corrcoef(lx, ly)[0, 1])
        flag = "  ←notable" if abs(r) >= 0.3 and len(lx) >= MIN_N else ""
        p(f"   prior mood → next-day #habits done   r={r:+.2f}  (n={len(lx)}){flag}")
    else:
        p(f"   not enough lagged day-pairs yet (n={len(lx)})")
    p("")

    # 5. Theme frequency + mood on theme-days
    p(f"⑤ Recurring themes (mentioned on ≥{MIN_N} days; mood those days vs overall):")
    overall_mood = _mean_sd([r["mood"] for r in dated.values()])[0]
    for label, pat in THEMES.items():
        rx = re.compile(pat, re.IGNORECASE)
        hit = [r for r in dated.values() if rx.search(r["text"])]
        if len(hit) < MIN_N:
            continue
        moods = [r["mood"] for r in hit if r["mood"] is not None]
        mm, mn = _mean_sd(moods)
        delta = (mm - overall_mood) if mm == mm else float("nan")
        p(
            f"   {label:22} {len(hit):2} days   mood {mm:4.1f} (n={mn:2})  Δ{delta:+.1f} vs {overall_mood:.1f}"
        )
    p("")

    # 6. Weight trajectory
    wser = sorted((d, r["weight"]) for d, r in dated.items() if r["weight"] is not None)
    if len(wser) >= 2:
        d0, w0 = wser[0]
        d1, w1 = wser[-1]
        span = (
            datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d0, "%Y-%m-%d")
        ).days or 1
        rate = (w1 - w0) / span * 7
        p("⑥ Weight trajectory:")
        p(
            f"   {w0:.1f} → {w1:.1f} kg over {span} days  ({rate:+.2f} kg/week, n={len(wser)})"
        )
        p("")

    # 7. Habit adherence (done vs missed logs)
    p(f"⑦ Habit adherence (logged done vs missed; min {MIN_N} tracked days):")
    done_c = defaultdict(int)
    miss_c = defaultdict(int)
    for r in dated.values():
        for h in r["habits"]:
            done_c[h] += 1
        for h in r["missed"]:
            miss_c[h] += 1
    tracked = [h for h in set(done_c) | set(miss_c) if done_c[h] + miss_c[h] >= MIN_N]
    for h in sorted(tracked, key=lambda h: -(done_c[h] + miss_c[h]))[:12]:
        tot = done_c[h] + miss_c[h]
        rate = done_c[h] / tot if tot else 0
        p(f"   {h[:34]:34} {done_c[h]:2}✓ {miss_c[h]:2}✗  ({rate:.0%} done)")
    return "\n".join(out)


def report_for(db_path: str = DB_PATH) -> str:
    """Build the deterministic mining report for a given DB — used by the /mine command
    and the weekly job so they share the CLI's exact logic."""
    return report(load_days(sqlite3.connect(db_path)))


def build_findings(days: dict) -> list[Finding]:
    """The stats layer for LLM synthesis: every candidate finding, gated BEFORE
    it can reach a prompt. A row only gets verdict="report" if it clears both
    MIN_N_ADVICE and a 95% CI that excludes zero — nothing is filtered by
    eyeballing the prose after the fact. Zero-data metrics get verdict="alert"
    (a logging gap, not a null result) instead of being narrated over as if
    "no correlation found" were itself a finding."""
    dated = _dated_only(days)
    findings: list[Finding] = []

    # ② correlations
    for xk, yk, label, tag in [
        ("sleep", "mood", "sleep hrs ↔ mood", "correlational_same_session"),
        ("sleep", "energy", "sleep hrs ↔ energy", "correlational_same_session"),
        ("steps", "mood", "steps ↔ mood", "correlational_same_session"),
        ("steps", "energy", "steps ↔ energy", "correlational_same_session"),
        ("kcal", "energy", "food kcal ↔ energy", "correlational_same_session"),
        ("kcal", "mood", "food kcal ↔ mood", "correlational_same_session"),
        ("mood", "energy", "mood ↔ energy", "correlational_same_session"),
        ("weight", "mood", "weight ↔ mood", "correlational_same_session"),
    ]:
        fid = f"corr:{xk}_{yk}"
        r, n = _corr(dated, xk, yk)
        if n == 0 or r != r:
            findings.append(
                Finding(
                    fid,
                    label,
                    "no data",
                    n,
                    "data_pipeline",
                    "alert",
                    "zero paired observations",
                )
            )
            continue
        if n < MIN_N_ADVICE:
            findings.append(
                Finding(
                    fid,
                    label,
                    f"r={r:+.2f}",
                    n,
                    tag,
                    "suppress",
                    f"n={n} < {MIN_N_ADVICE}",
                )
            )
            continue
        ci = _corr_ci95(r, n)
        if not _ci_excludes_zero(ci):
            findings.append(
                Finding(
                    fid,
                    label,
                    f"r={r:+.2f}",
                    n,
                    tag,
                    "suppress",
                    f"95% CI {ci} crosses zero",
                )
            )
            continue
        findings.append(
            Finding(
                fid,
                label,
                f"r={r:+.2f} (95% CI {ci[0]:+.2f}..{ci[1]:+.2f})",
                n,
                tag,
                "report",
            )
        )

    # ③ habit ↔ same-day mood, as a mean-difference CI rather than a Pearson r
    all_habits: set[str] = set()
    for r in dated.values():
        all_habits |= r["habits"]
    for h in sorted(all_habits):
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
        fid = f"habit:{h}"
        if len(done) < MIN_N_ADVICE or not notd:
            # insufficient data, not a pipeline problem — still surfaced as
            # "suppress" (not dropped) so it can show up in the "still thin
            # on" gap list if nothing else clears the bar.
            findings.append(
                Finding(
                    fid,
                    h,
                    "insufficient",
                    len(done),
                    "same_day_confounded",
                    "suppress",
                    f"n={len(done)} < {MIN_N_ADVICE}",
                )
            )
            continue
        delta = float(np.mean(done) - np.mean(notd))
        ci = _mean_diff_ci95(done, notd)
        if not _ci_excludes_zero(ci):
            findings.append(
                Finding(
                    fid,
                    h,
                    f"Δ{delta:+.1f}",
                    len(done),
                    "same_day_confounded",
                    "suppress",
                    "95% CI on Δ crosses zero",
                )
            )
            continue
        findings.append(
            Finding(
                fid,
                h,
                f"mood Δ{delta:+.1f} on done-days",
                len(done),
                "same_day_confounded",
                "report",
            )
        )

    # ④ lagged mood → next-day habit count
    lag_x, lag_y = [], []
    for d in sorted(dated):
        prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        if prev in dated and dated[prev]["mood"] is not None:
            lag_x.append(dated[prev]["mood"])
            lag_y.append(len(dated[d]["habits"]))
    lx, ly = np.array(lag_x), np.array(lag_y)
    fid = "lag:mood_habits"
    label = "prior-day mood → next-day habit count"
    if len(lx) == 0:
        findings.append(
            Finding(
                fid,
                label,
                "no data",
                0,
                "data_pipeline",
                "alert",
                "no lagged day-pairs yet",
            )
        )
    elif len(lx) < MIN_N_ADVICE or lx.std() == 0 or ly.std() == 0:
        findings.append(
            Finding(
                fid,
                label,
                "insufficient",
                len(lx),
                "lagged_leading_indicator",
                "suppress",
                f"n={len(lx)} < {MIN_N_ADVICE}",
            )
        )
    else:
        r = float(np.corrcoef(lx, ly)[0, 1])
        ci = _corr_ci95(r, len(lx))
        if not _ci_excludes_zero(ci):
            findings.append(
                Finding(
                    fid,
                    label,
                    f"r={r:+.2f}",
                    len(lx),
                    "lagged_leading_indicator",
                    "suppress",
                    f"95% CI {ci} crosses zero",
                )
            )
        else:
            findings.append(
                Finding(
                    fid,
                    label,
                    f"r={r:+.2f} (95% CI {ci[0]:+.2f}..{ci[1]:+.2f})",
                    len(lx),
                    "lagged_leading_indicator",
                    "report",
                )
            )

    # ⑤ themes: hit-days vs non-hit-days mood delta
    for label, pat in THEMES.items():
        rx = re.compile(pat, re.IGNORECASE)
        hit = [r for r in dated.values() if rx.search(r["text"])]
        if not hit:
            continue  # never mentioned — nothing to gate, not worth a row
        hit_moods = [r["mood"] for r in hit if r["mood"] is not None]
        miss_moods = [
            r["mood"]
            for r in dated.values()
            if not rx.search(r["text"]) and r["mood"] is not None
        ]
        fid = f"theme:{label}"
        if len(hit_moods) < MIN_N_ADVICE or not miss_moods:
            findings.append(
                Finding(
                    fid,
                    label,
                    "insufficient",
                    len(hit_moods),
                    "same_day_confounded",
                    "suppress",
                    f"n={len(hit_moods)} < {MIN_N_ADVICE}",
                )
            )
            continue
        delta = float(np.mean(hit_moods) - np.mean(miss_moods))
        ci = _mean_diff_ci95(hit_moods, miss_moods)
        if not _ci_excludes_zero(ci):
            findings.append(
                Finding(
                    fid,
                    label,
                    f"Δ{delta:+.1f}",
                    len(hit_moods),
                    "same_day_confounded",
                    "suppress",
                    "95% CI on Δ crosses zero",
                )
            )
            continue
        findings.append(
            Finding(
                fid,
                label,
                f"mood Δ{delta:+.1f} vs other days",
                len(hit_moods),
                "same_day_confounded",
                "report",
            )
        )

    # ⑥ weight trajectory — a genuine trend over the full tracked period
    wser = sorted((d, r["weight"]) for d, r in dated.items() if r["weight"] is not None)
    if len(wser) >= max(5, MIN_N_ADVICE // 2):
        d0, w0 = wser[0]
        d1, w1 = wser[-1]
        span = (
            datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d0, "%Y-%m-%d")
        ).days or 1
        rate = (w1 - w0) / span * 7
        findings.append(
            Finding(
                "trend:weight",
                "weight trajectory",
                f"{rate:+.2f} kg/week over {span}d",
                len(wser),
                "longitudinal_trend",
                "report",
            )
        )
    elif wser:
        findings.append(
            Finding(
                "trend:weight",
                "weight trajectory",
                "too few weigh-ins",
                len(wser),
                "longitudinal_trend",
                "suppress",
                f"n={len(wser)} weigh-ins",
            )
        )

    # ⑦ habit adherence — descriptive only, no correlation/causal claim possible
    done_c: dict[str, int] = defaultdict(int)
    miss_c: dict[str, int] = defaultdict(int)
    for r in dated.values():
        for h in r["habits"]:
            done_c[h] += 1
        for h in r["missed"]:
            miss_c[h] += 1
    for h in sorted(set(done_c) | set(miss_c)):
        tot = done_c[h] + miss_c[h]
        if tot < MIN_N_ADVICE:
            continue
        rate = done_c[h] / tot
        findings.append(
            Finding(
                f"adherence:{h}",
                h,
                f"{rate:.0%} done ({done_c[h]}/{tot})",
                tot,
                "descriptive",
                "report",
            )
        )

    return findings


def load_affect_pairs(c, max_gap_minutes: float = AFFECT_MAX_GAP_MIN) -> list[dict]:
    """Each voice note's affect_features joined to its nearest self_mood_rating tap.

    This is the check affect.py's docstring calls for: the local prosodic proxy is
    collection-only until it's been compared against the mood the user actually
    reported. Pairs wider than max_gap_minutes apart are dropped — a tap 3 hours
    later isn't ground truth for that specific note."""
    moods = [
        (datetime.fromisoformat(ts), float(val))
        for ts, val in c.execute(
            "SELECT ts, value FROM metrics WHERE key = 'self_mood_rating'"
        )
    ]
    pairs = []
    for ts, extra in c.execute(
        "SELECT ts, extra FROM entries WHERE extra LIKE '%affect_features%'"
    ):
        try:
            features = json.loads(extra)["affect_features"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        entry_ts = datetime.fromisoformat(ts)
        nearest = min(
            moods, key=lambda m: abs((m[0] - entry_ts).total_seconds()), default=None
        )
        if nearest is None:
            continue
        gap_min = abs((nearest[0] - entry_ts).total_seconds()) / 60
        if gap_min > max_gap_minutes:
            continue
        pairs.append({**features, "mood": nearest[1], "gap_min": gap_min})
    return pairs


def affect_report(pairs: list[dict]) -> str:
    out: list[str] = []
    p = out.append
    p("═══ AFFECT PROXY REPORT ═══")
    p(
        f"Voice notes matched to a mood tap within {AFFECT_MAX_GAP_MIN:.0f} min: "
        f"{len(pairs)}\n"
    )
    if not pairs:
        p("   No matched pairs yet — need a voice note and a self_mood_rating tap")
        p("   close together in time.")
        return "\n".join(out)
    p("Feature ↔ self_mood_rating (r, n) — small-sample, treat as leads only:")
    for feat in AFFECT_FEATURES:
        xs, ys = [], []
        for pr in pairs:
            if pr.get(feat) is not None:
                xs.append(pr[feat])
                ys.append(pr["mood"])
        xs, ys = np.array(xs), np.array(ys)
        if len(xs) < 5 or xs.std() == 0 or ys.std() == 0:
            p(f"   {feat:14} r=  n/a  (n={len(xs)})")
            continue
        r = float(np.corrcoef(xs, ys)[0, 1])
        flag = "  ←notable" if abs(r) >= 0.3 and len(xs) >= MIN_N else ""
        p(f"   {feat:14} r={r:+.2f}  (n={len(xs)}){flag}")
    return "\n".join(out)


def affect_report_for(db_path: str = DB_PATH) -> str:
    """Build the affect-proxy report for a given DB — mirrors report_for."""
    return affect_report(load_affect_pairs(sqlite3.connect(db_path)))


def _findings_prompt_block(findings: list[Finding]) -> str:
    return "\n".join(
        f"[{f.id}] ({f.tag}) {f.label}: {f.stat} (n={f.n})" for f in findings
    )


def _validate_citations(text: str, valid_ids: set[str]) -> str:
    """Drop any bullet whose trailing [id] citation doesn't resolve to a real
    finding id — including bullets with no citation at all. This is the
    backstop against the failure mode that caused the fabricated weight
    correlation and the "notable" n=6 read: an LLM can still free-write a
    claim even when told not to, so anything it can't tie back to a specific
    row in the input gets cut rather than trusted."""
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines():
        stripped = line.strip()
        is_bullet = stripped.startswith(("-", "•", "*")) or bool(
            re.match(r"^\d+[.)]", stripped)
        )
        if not is_bullet:
            kept.append(line)
            continue
        m = re.search(r"\[([\w:.\- ]+)\]\s*$", stripped)
        if m and m.group(1) in valid_ids:
            kept.append(line)
        else:
            dropped += 1
    result = "\n".join(kept)
    if dropped:
        result += f"\n\n({dropped} uncited or unresolved claim(s) dropped.)"
    return result


async def advise(findings: list[Finding]) -> str:
    """LLM synthesis over ONLY the findings the stats layer already cleared
    (verdict == "report") — the gate happens before generation, not as a
    caveat bolted on after. The model must cite a finding id on every claim;
    citations that don't resolve are dropped post-hoc rather than trusted."""
    reportable = [f for f in findings if f.verdict == "report"]
    if not reportable:
        gaps = [f.label for f in findings if f.verdict in ("suppress", "alert")]
        gap_note = f" Still thin on: {', '.join(gaps[:6])}." if gaps else ""
        return (
            "Not enough data yet for a synthesis — nothing clears "
            f"n≥{MIN_N_ADVICE} with a 95% CI that excludes zero.{gap_note}"
        )

    valid_ids = {f.id for f in reportable}
    guidance = "\n".join(
        f"- {tag}: {TAG_GUIDANCE[tag]}"
        for tag in dict.fromkeys(f.tag for f in reportable)
    )
    prompt = (
        "These are pre-filtered findings from my personal-ops logs — every row already "
        "cleared a sample-size floor and a 95% CI that excludes zero, so treat all of them "
        "as real (not certain, but real). Do not use any number, correlation, or comparison "
        "that isn't in this list — if you don't have a finding for something, don't invent one.\n\n"
        f"Findings:\n{_findings_prompt_block(reportable)}\n\n"
        "What each finding's causal footing licenses in prose:\n"
        f"{guidance}\n\n"
        "Give me 4-6 specific, direct pieces of advice. End every bullet with its finding id "
        "in brackets, e.g. '... [corr:mood_energy]'. No moralizing, no therapy voice."
    )

    import anthropic

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _validate_citations(text, valid_ids)


async def advise_for(db_path: str = DB_PATH) -> str:
    """Build findings and run the synthesis for a given DB — used by /mine
    advise and the weekly job. Independent of report_for(): the advice path
    reads structured findings, never the printed report's free text."""
    findings = build_findings(load_days(sqlite3.connect(db_path)))
    return await advise(findings)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--advise", action="store_true", help="add an LLM synthesis of the numbers"
    )
    ap.add_argument(
        "--affect",
        action="store_true",
        help="report voice-note affect features vs self_mood_rating instead",
    )
    args = ap.parse_args()
    c = sqlite3.connect(DB_PATH)
    if args.affect:
        print(affect_report(load_affect_pairs(c)))
        return
    days = load_days(c)
    print(report(days))
    if args.advise:
        print("\n═══ SYNTHESIS ═══")
        print(await advise(build_findings(days)))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

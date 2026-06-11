import json
import re
from datetime import date, timedelta

from logs import Logs

SHABBAT = 5  # Saturday

_ABBR = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def format_habits_for_prompt(db) -> str:
    """Render tracked habits (grouped by section) for the planner context, from the DB.

    Replaces the old habits.md projection: the habits table is the single source of
    truth, and this is generated fresh at prompt time rather than via a file on disk.
    """
    rows = db.query(
        "SELECT section, name, days, cue FROM habits WHERE tracked = 1 ORDER BY position, id"
    )
    if not rows:
        return ""
    # Group by section (first-appearance order) so each header appears once even if a
    # habit's position sorts it apart from its section-mates.
    sections: dict[str, list[str]] = {}
    for r in rows:
        days = [int(d) for d in r["days"].split(",") if d != ""]
        tag = f" [{','.join(_ABBR[d] for d in sorted(days))}]" if days else ""
        cue = f" — cue: {r['cue']}" if r["cue"] else ""
        sections.setdefault(r["section"], []).append(f"- {r['name']}{tag}{cue}")
    out = ["## Habits (schedule — source of truth is the habits table)"]
    for section, items in sections.items():
        out.append(f"\n### {section}")
        out.extend(items)
    return "\n".join(out)


def _matches(template_name: str, logged: str) -> bool:
    t_words = {w for w in re.split(r"\W+", template_name.lower()) if len(w) >= 3}
    l_words = {w for w in re.split(r"\W+", logged.lower()) if len(w) >= 3}
    return bool(t_words & l_words)


def _logged_on(logs: Logs, d: date) -> list[str]:
    path = logs._jsonl_path(d)
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
            if e.get("tag") == "habit":
                result.append(e["content"].strip().lower())
        except Exception:
            pass
    return result


def load_habit_logs(logs: Logs, days: int = 400) -> dict[str, list[str]]:
    """All habit-log contents per day from SQLite in one query.

    Pass the result as `logged_by_day` to compute_streak / recent_chain /
    missed_last_due_day to batch many habits' lookbacks into a single read instead
    of one file open per day per habit.
    """
    start = date.today() - timedelta(days=days)
    by_day: dict[str, list[str]] = {}
    for r in logs.db.entries_for_range(start, date.today()):
        if r["tag"] == "habit":
            by_day.setdefault(r["date"], []).append(r["content"].strip().lower())
    return by_day


def _logged_for(
    logs: Logs, d: date, logged_by_day: dict[str, list[str]] | None
) -> list[str]:
    if logged_by_day is not None:
        return logged_by_day.get(d.isoformat(), [])
    return _logged_on(logs, d)


def _is_due(d: date, due_weekdays: list[int] | None) -> bool:
    """A day counts toward a habit only if it's not Shabbat and is a scheduled weekday."""
    if d.weekday() == SHABBAT:
        return False
    return due_weekdays is None or d.weekday() in due_weekdays


def compute_streak(
    logs: Logs,
    habit_name: str,
    lookback: int = 365,
    due_weekdays: list[int] | None = None,
    logged_by_day: dict[str, list[str]] | None = None,
) -> tuple[int, int]:
    """Return (current_streak, longest_streak).

    `due_weekdays` (0=Mon..6=Sun) lists the scheduled days; None = every non-Shabbat day.
    Non-due days (Shabbat, or unscheduled weekdays) are *bonus*: doing the habit on one
    extends the streak, but skipping it never breaks the streak. A single missed due day
    is forgiven (never miss twice); only two consecutive missed due days break the run.
    """
    today = date.today()
    current = 0
    longest = 0
    run = 0
    in_current = True
    consecutive_misses = 0

    for i in range(lookback):
        d = today - timedelta(days=i)
        done = any(_matches(habit_name, h) for h in _logged_for(logs, d, logged_by_day))
        # Non-due days are *bonus*: quiet ones are transparent, done ones extend the run.
        if not _is_due(d, due_weekdays) and not done:
            continue
        if done:
            run += 1
            consecutive_misses = 0
            if in_current:
                current = run
        else:
            consecutive_misses += 1
            if consecutive_misses >= 2:
                if in_current:
                    in_current = False
                longest = max(longest, run)
                run = 0
                consecutive_misses = 0

    return current, max(longest, run)


def recent_chain(
    logs: Logs,
    habit_name: str,
    due_weekdays: list[int] | None = None,
    n: int = 14,
    lookback: int = 400,
    logged_by_day: dict[str, list[str]] | None = None,
) -> list[bool]:
    """Done/not-done for the last `n` DUE days, oldest→newest — the 'don't break the
    chain' visual. Off/Shabbat days are skipped so the chain is pure hits and misses."""
    today = date.today()
    chain: list[bool] = []
    for i in range(lookback):
        if len(chain) >= n:
            break
        d = today - timedelta(days=i)
        if not _is_due(d, due_weekdays):
            continue
        chain.append(
            any(_matches(habit_name, h) for h in _logged_for(logs, d, logged_by_day))
        )
    chain.reverse()
    return chain


def struggling_habits(
    logs: Logs,
    window: int = 14,
    threshold: float = 0.5,
    logged_by_day: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Tracked habits whose completion over the last `window` due days is below
    `threshold` — the 'these need a strategy' set. Reads the habits table directly.

    Excludes habits never once done (longest streak 0): those are not-yet-started,
    not failing. Returns worst-first dicts with the stats the strategy call needs.
    """
    if logged_by_day is None:
        logged_by_day = load_habit_logs(logs)
    rows = logs.db.query(
        "SELECT name, days, cue, identity FROM habits WHERE tracked = 1"
    )
    out = []
    for r in rows:
        due = [int(d) for d in r["days"].split(",") if d != ""] or None
        chain = recent_chain(
            logs, r["name"], due, n=window, logged_by_day=logged_by_day
        )
        if not chain:
            continue
        rate = sum(chain) / len(chain)
        if rate >= threshold:
            continue
        cur, longest = compute_streak(
            logs, r["name"], due_weekdays=due, logged_by_day=logged_by_day
        )
        if longest == 0:  # never started — not a failing habit, just an unbegun one
            continue
        out.append(
            {
                "name": r["name"],
                "done": sum(chain),
                "of": len(chain),
                "rate": round(rate, 2),
                "current_streak": cur,
                "best_streak": longest,
                "cue": r["cue"] or "",
                "identity": r["identity"] or "",
            }
        )
    out.sort(key=lambda x: x["rate"])
    return out


def missed_last_due_day(
    logs: Logs,
    habit_name: str,
    due_weekdays: list[int] | None = None,
    lookback: int = 400,
    logged_by_day: dict[str, list[str]] | None = None,
) -> bool:
    """True if the most recent prior due day was missed — the 'never miss twice' trigger."""
    today = date.today()
    for i in range(1, lookback):
        d = today - timedelta(days=i)
        if not _is_due(d, due_weekdays):
            continue
        return not any(
            _matches(habit_name, h) for h in _logged_for(logs, d, logged_by_day)
        )
    return False

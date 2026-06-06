import json
import re
from datetime import date, timedelta
from pathlib import Path

from logs import Logs

SHABBAT = 5  # Saturday


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
    """Return (current_streak, longest_streak), counting only due (non-Shabbat) days.

    `due_weekdays` (0=Mon..6=Sun) restricts which days count; None = every non-Shabbat
    day. A day the habit isn't scheduled for is neither a hit nor a break.
    """
    today = date.today()
    current = 0
    longest = 0
    run = 0
    in_current = True

    for i in range(lookback):
        d = today - timedelta(days=i)
        if not _is_due(d, due_weekdays):
            continue
        done = any(_matches(habit_name, h) for h in _logged_for(logs, d, logged_by_day))
        if done:
            run += 1
            if in_current:
                current = run
        else:
            if in_current:
                in_current = False
            longest = max(longest, run)
            run = 0

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
    rows = logs.db.query("SELECT name, days, cue, identity FROM habits WHERE tracked = 1")
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


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\|[\s\-:|]+\|", line.strip()))


def _fill_table(template_text: str, logs: Logs, target_date: date) -> str:
    lines = template_text.splitlines()
    result = []
    past_separator = False

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            past_separator = False
            result.append(line)
            continue

        if _is_separator(stripped):
            past_separator = True
            result.append(line)
            continue

        if not past_separator:
            result.append(line)
            continue

        # data row
        cells = stripped.strip("|").split("|")
        if len(cells) < 5:
            result.append(line)
            continue

        habit_name = cells[0].strip()
        streak_req = cells[4].strip() if len(cells) > 4 else ""
        notes = cells[5].strip() if len(cells) > 5 else ""

        logged_today = _logged_on(logs, target_date)
        done = 1 if any(_matches(habit_name, h) for h in logged_today) else 0
        cur, lon = compute_streak(logs, habit_name)

        new_cells = [
            f" {habit_name} ",
            f" {done} ",
            f" {cur} ",
            f" {lon} ",
            f" {streak_req} ",
            f" {notes} ",
        ]
        result.append("|" + "|".join(new_cells) + "|")

    return "\n".join(result)


def generate_habit_log(
    logs: Logs, template_path: Path, output_dir: Path, target_date: date
) -> Path:
    if not template_path.exists():
        raise FileNotFoundError(f"Habit template not found: {template_path}")
    template = template_path.read_text()
    filled = template.replace("{{DATE}}", str(target_date))
    filled = _fill_table(filled, logs, target_date)
    output_dir.mkdir(exist_ok=True)
    out = output_dir / f"{target_date}-habits.md"
    out.write_text(filled)
    return out

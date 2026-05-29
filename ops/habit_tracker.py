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


def compute_streak(logs: Logs, habit_name: str, lookback: int = 365) -> tuple[int, int]:
    """Return (current_streak, longest_streak), skipping Shabbat."""
    today = date.today()
    current = 0
    longest = 0
    run = 0
    in_current = True

    for i in range(lookback):
        d = today - timedelta(days=i)
        if d.weekday() == SHABBAT:
            continue
        done = any(_matches(habit_name, h) for h in _logged_on(logs, d))
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


def generate_habit_log(logs: Logs, template_path: Path, output_dir: Path, target_date: date) -> Path:
    if not template_path.exists():
        raise FileNotFoundError(f"Habit template not found: {template_path}")
    template = template_path.read_text()
    filled = template.replace("{{DATE}}", str(target_date))
    filled = _fill_table(filled, logs, target_date)
    output_dir.mkdir(exist_ok=True)
    out = output_dir / f"{target_date}-habits.md"
    out.write_text(filled)
    return out

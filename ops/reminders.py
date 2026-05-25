import json
import uuid
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

REMINDERS_FILE = Path(__file__).parent / "reminders.json"
TZ = ZoneInfo("Asia/Jerusalem")


def load():
    if not REMINDERS_FILE.exists():
        return []
    return json.loads(REMINDERS_FILE.read_text())


def save(reminders):
    REMINDERS_FILE.write_text(json.dumps(reminders, indent=2))


def add(text, reminder_type, **kwargs):
    reminders = load()
    entry = {"id": str(uuid.uuid4()), "text": text, "type": reminder_type, **kwargs}
    reminders.append(entry)
    save(reminders)
    return entry


def remove(reminder_id):
    reminders = [r for r in load() if r["id"] != reminder_id]
    save(reminders)


def due_now(reminders):
    """Return (due_reminders, updated_reminders_list) — one-time reminders are removed after firing."""
    now = datetime.now(TZ)
    today = now.date().isoformat()
    current_time = now.time().replace(second=0, microsecond=0)
    current_minutes = now.hour * 60 + now.minute
    due = []
    remaining = []

    for r in reminders:
        fired = False

        if r["type"] == "once":
            h, m = map(int, r["time"].split(":"))
            if current_time == time(h, m) and r.get("date", today) == today:
                due.append(r)
                fired = True

        elif r["type"] == "daily":
            h, m = map(int, r["time"].split(":"))
            if current_time == time(h, m):
                due.append(r)

        elif r["type"] == "interval":
            interval = r["interval_minutes"]
            start_h, start_m = map(int, r.get("window_start", "08:00").split(":"))
            end_h, end_m = map(int, r.get("window_end", "22:00").split(":"))
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            if start_minutes <= current_minutes <= end_minutes:
                if (current_minutes - start_minutes) % interval == 0:
                    due.append(r)

        if not fired:
            remaining.append(r)

    if len(remaining) != len(reminders):
        save(remaining)

    return due

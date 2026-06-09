import os
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from db import Database

TZ = ZoneInfo("Asia/Jerusalem")
# Resolve relative to this file, not the process CWD: getcwd() silently pointed
# at a different DB if the bot was launched from another directory.
_LOG_DIR = os.path.join(os.path.dirname(__file__), "log")


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["auto_log"] = bool(d.get("auto_log", 0))
    # Remove None/empty fields to keep the interface clean
    return {k: v for k, v in d.items() if v is not None and v != ""}


class Reminders:
    def __init__(self, file_path=None, db_path=None):
        # Resolve the DB path. Tests pass an isolated `file_path` (e.g. a tmp
        # dir); keep the SQLite DB beside it so they never touch the real
        # ops.db. Prod calls Reminders() with no args → the real DB.
        if db_path is None:
            if file_path is not None:
                db_path = os.path.join(os.path.dirname(str(file_path)), "ops.db")
            else:
                db_path = os.path.join(_LOG_DIR, "ops.db")
        self.db = Database(db_path)

    def load(self) -> list:
        return [_row_to_dict(r) for r in self.db.get_reminders()]

    def save(self, reminders: list):
        self.db.save_reminders(reminders)

    def add(self, text: str, reminder_type: str, **kwargs) -> dict:
        entry = {"id": str(uuid.uuid4()), "text": text, "type": reminder_type, **kwargs}
        self.db.add_reminder(entry)
        return entry

    def remove(self, reminder_id: str):
        self.db.remove_reminder(reminder_id)

    def due_now(self) -> list:
        reminders = self.load()
        now = datetime.now(TZ)
        today = now.date().isoformat()
        current_time = now.time().replace(second=0, microsecond=0)
        current_minutes = now.hour * 60 + now.minute
        due = []
        remaining = []

        for r in reminders:
            fired = False

            if r["type"] == "once":
                # If date is missing, don't fire — safer than defaulting to today
                reminder_date = r.get("date", "")
                if not reminder_date:
                    remaining.append(r)
                    continue
                h, m = map(int, r["time"].split(":"))
                if current_time == time(h, m) and reminder_date == today:
                    due.append(r)
                    fired = True

            elif r["type"] == "daily":
                h, m = map(int, r["time"].split(":"))
                if current_time == time(h, m):
                    due.append(r)

            elif r["type"] == "weekly":
                h, m = map(int, r["time"].split(":"))
                if now.weekday() == r["day"] and current_time == time(h, m):
                    due.append(r)

            elif r["type"] == "interval":
                interval = r["interval_minutes"]
                start_h, start_m = map(int, r.get("window_start", "08:00").split(":"))
                end_h, end_m = map(int, r.get("window_end", "22:00").split(":"))
                start_min = start_h * 60 + start_m
                end_min = end_h * 60 + end_m
                if start_min <= current_minutes <= end_min:
                    if (current_minutes - start_min) % interval == 0:
                        due.append(r)

            if not fired:
                remaining.append(r)

        if len(remaining) != len(reminders):
            self.save(remaining)

        return due

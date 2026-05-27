import json
import uuid
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")


class Reminders:
    def __init__(self, file_path: Path | None = None):
        self.file_path = file_path or Path(__file__).parent / "reminders.json"

    def load(self) -> list:
        if not self.file_path.exists():
            return []
        return json.loads(self.file_path.read_text())

    def save(self, reminders: list):
        self.file_path.write_text(json.dumps(reminders, indent=2))

    def add(self, text: str, reminder_type: str, **kwargs) -> dict:
        reminders = self.load()
        entry = {"id": str(uuid.uuid4()), "text": text, "type": reminder_type, **kwargs}
        reminders.append(entry)
        self.save(reminders)
        return entry

    def remove(self, reminder_id: str):
        self.save([r for r in self.load() if r["id"] != reminder_id])

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

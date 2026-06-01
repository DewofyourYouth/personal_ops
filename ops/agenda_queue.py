import json
import os
from datetime import date
from pathlib import Path


class AgendaQueue:
    def __init__(self, log_dir: str):
        self.path = Path(log_dir) / "agenda-queue.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return []

    def save(self, items: list[dict]):
        self.path.write_text(json.dumps(items, indent=2))

    def add(self, text: str, target_date: date) -> dict:
        items = self.load()
        entry = {"date": target_date.isoformat(), "text": text, "queued_at": date.today().isoformat()}
        items.append(entry)
        self.save(items)
        return entry

    def pop_for_today(self) -> list[str]:
        """Remove and return all items queued for today."""
        items = self.load()
        today = date.today().isoformat()
        due = [i["text"] for i in items if i["date"] == today]
        remaining = [i for i in items if i["date"] != today]
        if due:
            self.save(remaining)
        return due

    def pending(self) -> list[dict]:
        today = date.today().isoformat()
        return [i for i in self.load() if i["date"] >= today]

import json
import uuid
from datetime import date
from pathlib import Path


class Backlog:
    def __init__(self, log_dir: str):
        self.path = Path(log_dir) / "backlog.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return []

    def save(self, items: list[dict]):
        self.path.write_text(json.dumps(items, indent=2))

    def add(self, text: str) -> dict:
        items = self.load()
        entry = {"id": str(uuid.uuid4())[:8], "text": text.strip(), "added": date.today().isoformat()}
        items.append(entry)
        self.save(items)
        return entry

    def remove(self, item_id: str) -> bool:
        items = self.load()
        remaining = [i for i in items if i["id"] != item_id]
        if len(remaining) == len(items):
            return False
        self.save(remaining)
        return True

    def get(self, item_id: str) -> dict | None:
        return next((i for i in self.load() if i["id"] == item_id), None)

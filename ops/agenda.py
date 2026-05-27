import json
import os
from datetime import date


class Agenda:
    def __init__(self, log_dir: str):
        self.log_dir = log_dir

    def load(self) -> dict:
        path = self._path()
        if not os.path.exists(path):
            return {"items": []}
        with open(path) as f:
            return json.load(f)

    def save(self, data: dict):
        with open(self._path(), "w") as f:
            json.dump(data, f, indent=2)

    def accept_items(self, texts: list, source: str = "llm") -> list:
        data = self.load()
        start = len(data["items"])
        new_items = [
            {"id": start + i, "text": text, "status": "open", "source": source}
            for i, text in enumerate(texts)
        ]
        data["items"].extend(new_items)
        self.save(data)
        return new_items

    def edit_item(self, item_id: int, text: str):
        data = self.load()
        for item in data["items"]:
            if item["id"] == item_id:
                item["text"] = text
                break
        self.save(data)

    def mark_status(self, item_id: int, status: str):
        data = self.load()
        for item in data["items"]:
            if item["id"] == item_id:
                item["status"] = status
                break
        self.save(data)

    def get_status(self) -> list:
        return [i for i in self.load()["items"]]

    def get_open(self) -> list:
        return [i for i in self.load()["items"] if i["status"] == "open"]

    def existing_summary(self) -> str:
        items = self.load()["items"]
        done = [i["text"] for i in items if i["status"] in ("done", "missed")]
        open_ = [i["text"] for i in items if i["status"] == "open"]
        parts = []
        if done:
            parts.append("Already completed/missed:\n" + "\n".join(f"- {t}" for t in done))
        if open_:
            parts.append("Still open:\n" + "\n".join(f"- {t}" for t in open_))
        return "\n\n".join(parts)

    async def generate(self, planner, calendar_events: str = "") -> list[str]:
        return await planner.propose(calendar_events, self.existing_summary())

    def write_to_markdown(self, items: list):
        log_file = os.path.join(self.log_dir, f"{date.today()}.md")
        lines = ["\n## Agenda\n"] + [f"- [ ] {item['text']}" for item in items] + [""]
        with open(log_file, "a") as f:
            f.write("\n".join(lines))

    def _path(self) -> str:
        return os.path.join(self.log_dir, f"{date.today()}-agenda.json")

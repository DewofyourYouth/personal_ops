import json
import os
from datetime import date


def _path(log_dir):
    return os.path.join(log_dir, f"{date.today()}-agenda.json")


def load(log_dir):
    path = _path(log_dir)
    if not os.path.exists(path):
        return {"items": []}
    with open(path) as f:
        return json.load(f)


def _save(log_dir, data):
    with open(_path(log_dir), "w") as f:
        json.dump(data, f, indent=2)


def accept_items(log_dir, texts, source="llm"):
    data = load(log_dir)
    start = len(data["items"])
    new_items = [
        {"id": start + i, "text": text, "status": "open", "source": source}
        for i, text in enumerate(texts)
    ]
    data["items"].extend(new_items)
    _save(log_dir, data)
    return new_items


def edit_item(log_dir, item_id, text):
    data = load(log_dir)
    for item in data["items"]:
        if item["id"] == item_id:
            item["text"] = text
            break
    _save(log_dir, data)


def mark_status(log_dir, item_id, status):
    data = load(log_dir)
    for item in data["items"]:
        if item["id"] == item_id:
            item["status"] = status
            break
    _save(log_dir, data)


def get_open(log_dir):
    return [i for i in load(log_dir)["items"] if i["status"] == "open"]


def write_to_markdown(log_dir, items):
    log_file = os.path.join(log_dir, f"{date.today()}.md")
    lines = ["\n## Agenda\n"] + [f"- [ ] {item['text']}" for item in items] + [""]
    with open(log_file, "a") as f:
        f.write("\n".join(lines))

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")


class Logs:
    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

    # --- Writing ---

    def write(self, tag: str, content: str, extra: dict | None = None):
        entry = {
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "tag": tag,
            "content": content,
            **(extra or {}),
        }
        with open(self._jsonl_path(date.today()), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write_metric(self, key: str, value, unit: str = ""):
        self.write("metric", f"{key} {value}{unit}", extra={"key": key, "value": value, "unit": unit})

    # --- Reading ---

    def read_today(self) -> list[dict]:
        path = self._jsonl_path(date.today())
        if not os.path.exists(path):
            return []
        entries = []
        for line in open(path):
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return entries

    def read_recent(self, days: int = 3) -> str:
        sections = []
        for i in range(1, days + 1):
            d = date.today() - timedelta(days=i)
            lines = self._read_day(d)
            if lines:
                sections.append(f"### {d}\n" + "\n".join(lines))
        return "\n\n".join(sections) if sections else "No recent logs."

    def load_metrics(self, days: int = 14) -> dict[str, list]:
        from collections import defaultdict
        result: dict = defaultdict(list)
        for i in range(days, -1, -1):
            d = date.today() - timedelta(days=i)
            path = self._jsonl_path(d)
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                try:
                    e = json.loads(line)
                    if e.get("tag") == "metric":
                        unit = e.get("unit", "")
                        display = f"{e['value']}{unit}" if unit else e["value"]
                        result[e["key"]].append((str(d), display))
                except Exception:
                    pass
        return dict(result)

    def format_metrics_for_prompt(self, days: int = 14) -> str:
        data = self.load_metrics(days=days)
        if not data:
            return ""
        lines = ["Tracked metrics:"]
        for key, entries in sorted(data.items()):
            recent = ", ".join(f"{d}: {v}" for d, v in entries[-7:])
            lines.append(f"  {key}: {recent}")
        return "\n".join(lines)

    # --- Internal ---

    def _jsonl_path(self, d: date) -> Path:
        return Path(self.log_dir) / f"{d}.jsonl"

    def _read_day(self, d: date) -> list[str]:
        jsonl = self._jsonl_path(d)
        md = Path(self.log_dir) / f"{d}.md"
        if jsonl.exists():
            lines = []
            for line in jsonl.read_text().splitlines():
                try:
                    e = json.loads(line)
                    if e.get("tag") != "metric":
                        lines.append(f"[{e['ts']}] #{e['tag']}: {e['content']}")
                except Exception:
                    pass
            return lines
        if md.exists():
            return self._parse_md(md.read_text())
        return []

    @staticmethod
    def _parse_md(text: str) -> list[str]:
        lines = []
        tag = content = None
        for line in text.splitlines():
            m = re.match(r"^## (\d{2}:\d{2}) (#\w+)$", line)
            if m:
                if tag and content:
                    lines.append(f"[{m.group(1)}] {tag}: {content}")
                tag, content = m.group(2), ""
            elif tag is not None:
                stripped = line.strip()
                if stripped and not stripped.startswith("- [ ]") and stripped != "## Agenda":
                    content = (content + " " + stripped).strip()
        if tag and content:
            lines.append(f"[?] {tag}: {content}")
        return lines

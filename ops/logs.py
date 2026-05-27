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
        for i in range(days, -1, -1):
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

    # --- Stats ---

    _ANCHOR_KEYWORDS = {
        "meds", "shacharit", "davening", "chavrusa", "daf yomi", "daf",
        "yoma", "yerushalmi", "walk", "anki", "strength",
    }

    def compute_stats(self, days: int = 7) -> dict[str, dict]:
        stats = {}
        for i in range(days - 1, -1, -1):
            d = date.today() - timedelta(days=i)
            s: dict = {
                "completion": None,   # (done, total)
                "anchors": None,      # (done, total)
                "wins": 0,
                "reminders": 0,
                "checkins": 0,
                "responded": 0,       # reminders responded to within 15 min
            }

            # Agenda completion
            agenda_path = Path(self.log_dir) / f"{d}-agenda.json"
            if agenda_path.exists():
                try:
                    items = json.loads(agenda_path.read_text()).get("items", [])
                    resolved = [i for i in items if i["status"] in ("done", "missed")]
                    if resolved:
                        done = sum(1 for i in resolved if i["status"] == "done")
                        s["completion"] = (done, len(resolved))
                    anchors = [i for i in items if any(kw in i["text"].lower() for kw in self._ANCHOR_KEYWORDS)]
                    resolved_anchors = [i for i in anchors if i["status"] in ("done", "missed")]
                    if resolved_anchors:
                        s["anchors"] = (sum(1 for i in resolved_anchors if i["status"] == "done"), len(resolved_anchors))
                except Exception:
                    pass

            # Log entry stats
            jsonl = self._jsonl_path(d)
            if jsonl.exists():
                entries = []
                for line in jsonl.read_text().splitlines():
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
                s["wins"] = sum(1 for e in entries if e.get("tag") == "win")
                reminder_times = [datetime.fromisoformat(e["ts"]) for e in entries if e.get("tag") == "reminder"]
                checkin_times  = [datetime.fromisoformat(e["ts"]) for e in entries if e.get("tag") == "checkin"]
                s["reminders"] = len(reminder_times)
                s["checkins"]  = len(checkin_times)
                if reminder_times:
                    s["responded"] = sum(
                        1 for rt in reminder_times
                        if any(timedelta(0) <= ct - rt <= timedelta(minutes=15) for ct in checkin_times)
                    )

            stats[str(d)] = s
        return stats

    def format_stats_for_prompt(self, days: int = 7) -> str:
        stats = self.compute_stats(days=days)
        days_with_data = [s for s in stats.values() if s["completion"] or s["wins"]]
        if not days_with_data:
            return ""

        lines = ["## Daily stats\n"]
        lines.append("| Date | Completion | Anchors | Wins | Checkin response |")
        lines.append("|------|------------|---------|------|-----------------|")

        for date_str, s in stats.items():
            comp = f"{s['completion'][0]}/{s['completion'][1]} ({100*s['completion'][0]//s['completion'][1]}%)" if s["completion"] else "—"
            anch = f"{s['anchors'][0]}/{s['anchors'][1]} ({100*s['anchors'][0]//s['anchors'][1]}%)" if s["anchors"] else "—"
            wins = str(s["wins"]) if s["wins"] else "—"
            if s["reminders"] == 0:
                cr = "—"
            else:
                cr = f"{s['responded']}/{s['reminders']}"
            lines.append(f"| {date_str} | {comp} | {anch} | {wins} | {cr} |")

        # Rolling averages over days with completion data
        comp_days = [s["completion"] for s in stats.values() if s["completion"]]
        anch_days = [s["anchors"]    for s in stats.values() if s["anchors"]]
        total_wins = sum(s["wins"] for s in stats.values())
        total_rem  = sum(s["reminders"] for s in stats.values())
        total_resp = sum(s["responded"] for s in stats.values())

        summary = []
        if comp_days:
            avg = sum(d / t for d, t in comp_days) / len(comp_days)
            summary.append(f"avg completion {avg:.0%}")
        if anch_days:
            avg = sum(d / t for d, t in anch_days) / len(anch_days)
            summary.append(f"avg anchor rate {avg:.0%}")
        if total_wins:
            summary.append(f"{total_wins} wins logged")
        if total_rem:
            summary.append(f"checkin response {total_resp}/{total_rem}")
        if summary:
            lines.append("\n**Rolling (" + str(days) + " days):** " + ", ".join(summary))

        return "\n".join(lines)

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

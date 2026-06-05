import os
import re
from pathlib import Path

CONTEXT_DIR = (
    Path(os.environ["OPS_CONTEXT_DIR"])
    if "OPS_CONTEXT_DIR" in os.environ
    else Path(__file__).parent / "context"
)
CONTEXT_FILES = [
    "goals.md",
    "priorities.md",
    "constraints.md",
    "habits.md",
    "projects.md",
    "principles.md",
    "bot-personality.md",
    "review-rules.md",
    "agenda-rules.md",
]


class Context:
    def __init__(self, context_dir: Path | None = None):
        self.dir = context_dir or CONTEXT_DIR
        self.dir.mkdir(exist_ok=True)

    def load_all(self) -> str:
        parts = []
        for fname in CONTEXT_FILES:
            path = self.dir / fname
            if path.exists():
                parts.append(f"### {fname}\n{path.read_text().strip()}")
        return "\n\n".join(parts)

    def read(self, filename: str) -> str:
        path = self.dir / filename
        return path.read_text().strip() if path.exists() else ""

    def write(self, filename: str, content: str):
        (self.dir / filename).write_text(content)

    def files(self) -> list[str]:
        return CONTEXT_FILES

    _DAY_NAMES = {"sun": 6, "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5}

    def parse_habits(self) -> dict[str, list[dict]]:
        """Return {section: [{"raw": str, "text": str, "days": list[int]|None}]}.

        "days" is a list of weekday ints (0=Mon…6=Sun) when the habit has a [day,day] tag,
        or None meaning every day. Excludes 'Always off' section entirely.
        """
        text = self.read("habits.md")
        if not text:
            return {}
        sections: dict[str, list[dict]] = {}
        current: str | None = None
        for line in text.splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
                current = None if "always off" in section.lower() else section
                if current and current not in sections:
                    sections[current] = []
            elif re.match(r"^\s*- ", line) and current is not None:
                raw = re.sub(r"^\s*- ", "", line).strip()
                tag_m = re.search(r"\[([^\]]+)\]$", raw)
                if tag_m:
                    days = [
                        self._DAY_NAMES[d.strip()]
                        for d in tag_m.group(1).split(",")
                        if d.strip() in self._DAY_NAMES
                    ]
                    text_clean = raw[: tag_m.start()].strip().rstrip("—").strip()
                else:
                    days = None
                    text_clean = raw
                sections[current].append({"raw": raw, "text": text_clean, "days": days})
        return sections

    @staticmethod
    def habit_display_name(text: str) -> str:
        """Strip time prefix, parenthetical notes, and '— ...' suffix for display."""
        name = re.sub(r"^\d{1,2}:\d{2}(?:–\d{1,2}:\d{2})?\s*", "", text)
        name = name.split(" — ")[0].split(" (")[0].strip()
        return name or text

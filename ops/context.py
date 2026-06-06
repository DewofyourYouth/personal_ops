import os
import re
from pathlib import Path

CONTEXT_DIR = (
    Path(os.environ["OPS_CONTEXT_DIR"])
    if "OPS_CONTEXT_DIR" in os.environ
    else Path(__file__).parent / "context"
)
# habits.md is intentionally absent: the habits table is the single source of truth,
# and the planner gets the schedule via habit_tracker.format_habits_for_prompt(db).
CONTEXT_FILES = [
    "goals.md",
    "priorities.md",
    "constraints.md",
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

    @staticmethod
    def habit_display_name(text: str) -> str:
        """Strip time prefix, parenthetical notes, and '— ...' suffix for display."""
        name = re.sub(r"^\d{1,2}:\d{2}(?:–\d{1,2}:\d{2})?\s*", "", text)
        name = name.split(" — ")[0].split(" (")[0].strip()
        return name or text

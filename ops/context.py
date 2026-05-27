from pathlib import Path

CONTEXT_DIR = Path(__file__).parent / "context"
CONTEXT_FILES = ["goals.md", "priorities.md", "constraints.md", "projects.md", "principles.md"]


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

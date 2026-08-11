"""time_tracker.py — the live-timer half of time tracking.

Deterministic core, no Telegram/LLM concerns (this whole feature needs zero AI calls —
duration math and text parsing only). Owns one small table for the *in-progress* timer;
completed entries are plain `#time` log entries (`time_handlers.py` writes them via
`Logs.write`), same as every other tag, not a second table here.

Only one timer can run per chat at a time — `start()` uses INSERT OR REPLACE so it never
raises, but the "a timer's already running" business rule and user messaging live in
`time_handlers.py`, which checks `running()` before calling `start()`.
"""

from datetime import datetime

from logs import TZ

_RUNNING_DDL = """
CREATE TABLE IF NOT EXISTS time_running (
    chat_id     INTEGER PRIMARY KEY,
    start_ts    TEXT NOT NULL,
    description TEXT NOT NULL,
    project     TEXT NOT NULL DEFAULT ''
);
"""


class TimeTracker:
    def __init__(self, db) -> None:
        self.db = db
        self.db.ensure_schema(_RUNNING_DDL)

    def running(self, chat_id: int) -> dict | None:
        rows = self.db.query("SELECT * FROM time_running WHERE chat_id = ?", (chat_id,))
        return dict(rows[0]) if rows else None

    def start(self, chat_id: int, description: str, project: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO time_running "
            "(chat_id, start_ts, description, project) VALUES (?, ?, ?, ?)",
            (chat_id, datetime.now(TZ).isoformat(), description, project),
        )

    def stop(self, chat_id: int) -> dict | None:
        """Stop the running timer and return its elapsed minutes, or None if
        nothing was running. Does not log anything — the caller decides that."""
        row = self.running(chat_id)
        if row is None:
            return None
        self.db.execute("DELETE FROM time_running WHERE chat_id = ?", (chat_id,))
        started = datetime.fromisoformat(row["start_ts"])
        elapsed_minutes = (datetime.now(TZ) - started).total_seconds() / 60
        return {
            "description": row["description"],
            "project": row["project"],
            "minutes": elapsed_minutes,
            "start_ts": row["start_ts"],
        }

    def cancel(self, chat_id: int) -> dict | None:
        """Discard the running timer without logging. Returns the discarded row,
        or None if nothing was running."""
        row = self.running(chat_id)
        if row is None:
            return None
        self.db.execute("DELETE FROM time_running WHERE chat_id = ?", (chat_id,))
        return row

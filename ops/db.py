"""
db.py — SQLite database layer for personal_ops.

Replaces JSONL files as the primary read source. JSONL files continue to be
written in parallel for integrity debugging and human readability.

Schema:
  entries  — all log entries (tags: log, insight, habit, food, win, skip, etc.)
  metrics  — metric entries with key/value/unit (mood, energy, weight, steps, etc.)

Agenda items (-agenda.json), reminders (reminders.json), backlog (backlog.json),
and baseline (baseline.json) remain as JSON files for now — they have their own
read/write logic and are small enough that SQLite doesn't add much there yet.
"""

import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path


_CREATE_ENTRIES = """
CREATE TABLE IF NOT EXISTS entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    date    TEXT NOT NULL,
    tag     TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);
CREATE INDEX IF NOT EXISTS idx_entries_tag  ON entries(tag);
"""

_CREATE_METRICS = """
CREATE TABLE IF NOT EXISTS metrics (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,
    date  TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    unit  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics(date);
CREATE INDEX IF NOT EXISTS idx_metrics_key  ON metrics(key);
"""


class Database:
    def __init__(self, db_path: str):
        self.path = db_path
        self._local = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init(self):
        conn = self._conn()
        conn.executescript(_CREATE_ENTRIES)
        conn.executescript(_CREATE_METRICS)
        conn.commit()

    def insert_entry(self, ts: str, date_str: str, tag: str, content: str):
        self._conn().execute(
            "INSERT INTO entries (ts, date, tag, content) VALUES (?, ?, ?, ?)",
            (ts, date_str, tag, content),
        )
        self._conn().commit()

    def insert_metric(self, ts: str, date_str: str, key: str, value: str, unit: str = ""):
        self._conn().execute(
            "INSERT INTO metrics (ts, date, key, value, unit) VALUES (?, ?, ?, ?, ?)",
            (ts, date_str, key, value, unit),
        )
        self._conn().commit()

    def entries_for_date(self, d: date) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM entries WHERE date = ? ORDER BY ts",
            (d.isoformat(),),
        ).fetchall()

    def entries_for_range(self, start: date, end: date) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM entries WHERE date >= ? AND date <= ? ORDER BY date, ts",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    def metrics_for_range(self, start: date, end: date) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM metrics WHERE date >= ? AND date <= ? ORDER BY date, ts",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    def metrics_max_per_day(self, start: date, end: date, key: str) -> list[sqlite3.Row]:
        """Return the highest numeric value per day for a given metric key.

        Used for step counts where multiple readings per day exist and the
        highest value is the most complete (end-of-day total wins).
        """
        return self._conn().execute(
            """
            SELECT date, key, CAST(MAX(CAST(value AS REAL)) AS INTEGER) as value, unit
            FROM metrics
            WHERE date >= ? AND date <= ? AND key = ?
            GROUP BY date
            ORDER BY date
            """,
            (start.isoformat(), end.isoformat(), key),
        ).fetchall()

    def earliest_entry_date(self) -> date | None:
        row = self._conn().execute("SELECT MIN(date) as d FROM entries").fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def earliest_entry_date_with_tag(self, tag: str) -> date | None:
        row = self._conn().execute(
            "SELECT MIN(date) as d FROM entries WHERE tag = ?", (tag,)
        ).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

"""
db.py — SQLite database layer for personal_ops.

Replaces JSONL files as the primary read source. JSONL files continue to be
written in parallel for integrity debugging and human readability.

Schema:
  entries          — all log entries (tags: log, insight, habit, food, win, skip, etc.)
  metrics          — metric entries with key/value/unit (mood, energy, weight, steps, etc.)
  job_applications — job search tracking (migrated from job_tracker CSV)

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

_CREATE_JOB_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS job_applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT DEFAULT '',
    applied_date TEXT DEFAULT '',
    status       TEXT DEFAULT 'applied',
    notes        TEXT DEFAULT '',
    source       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON job_applications(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON job_applications(company);
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
        conn.executescript(_CREATE_JOB_APPLICATIONS)
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

    # --- Job applications ---

    def upsert_job(self, company: str, title: str, url: str = "", applied_date: str = "",
                   status: str = "applied", notes: str = "", source: str = "") -> int:
        """Insert or update a job application. Matches on company+title."""
        conn = self._conn()
        existing = conn.execute(
            "SELECT id FROM job_applications WHERE company = ? AND title = ?",
            (company, title),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE job_applications SET url=?, applied_date=?, status=?, notes=?, source=? WHERE id=?",
                (url, applied_date, status, notes, source, existing["id"]),
            )
            conn.commit()
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO job_applications (company, title, url, applied_date, status, notes, source) VALUES (?,?,?,?,?,?,?)",
                (company, title, url, applied_date, status, notes, source),
            )
            conn.commit()
            return cur.lastrowid

    def get_jobs(self, status: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM job_applications"
        if status:
            return self._conn().execute(query + " WHERE status = ? ORDER BY applied_date DESC", (status,)).fetchall()
        return self._conn().execute(query + " ORDER BY applied_date DESC").fetchall()

    def update_job_status(self, job_id: int, status: str, notes: str = "") -> bool:
        conn = self._conn()
        conn.execute("UPDATE job_applications SET status=?, notes=? WHERE id=?", (status, notes, job_id))
        conn.commit()
        return conn.execute("SELECT changes()").fetchone()[0] > 0

    def earliest_entry_date(self) -> date | None:
        row = self._conn().execute("SELECT MIN(date) as d FROM entries").fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def earliest_entry_date_with_tag(self, tag: str) -> date | None:
        row = self._conn().execute(
            "SELECT MIN(date) as d FROM entries WHERE tag = ?", (tag,)
        ).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

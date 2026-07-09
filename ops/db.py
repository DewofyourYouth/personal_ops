"""
db.py — SQLite database layer for personal_ops.

Replaces JSONL files as the primary read source. JSONL files continue to be
written in parallel for integrity debugging and human readability.

Schema:
  entries          — all log entries (tags: log, insight, habit, food, win, skip, etc.)
  metrics          — metric entries with key/value/unit (mood, energy, weight, steps, etc.)
  reminders        — scheduled reminders (daily, interval, once, weekly)

Agenda items (-agenda.json), reminders (reminders.json), backlog (backlog.json),
and baseline (baseline.json) remain as JSON files for now — they have their own
read/write logic and are small enough that SQLite doesn't add much there yet.
"""

import sqlite3
import threading
from datetime import date


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

_CREATE_REMINDERS = """
CREATE TABLE IF NOT EXISTS reminders (
    id               TEXT PRIMARY KEY,
    text             TEXT NOT NULL,
    type             TEXT NOT NULL,
    date             TEXT DEFAULT '',
    time             TEXT DEFAULT '',
    day              INTEGER DEFAULT NULL,
    interval_minutes INTEGER DEFAULT NULL,
    window_start     TEXT DEFAULT '08:00',
    window_end       TEXT DEFAULT '22:00',
    auto_log         INTEGER DEFAULT 0
);
"""


# Append-only correction/confirmation events for classifier labels. A row is
# training data ("this text should be labelled X") and an audit trail — the
# original entry's first label survives here even after entries.tag is updated
# to the corrected value for the rest of the app to read.
_CREATE_LABEL_EVENTS = """
CREATE TABLE IF NOT EXISTS label_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    ref_entry_id INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    from_label   TEXT NOT NULL,
    to_label     TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'user_tap'
);
CREATE INDEX IF NOT EXISTS idx_label_events_ref ON label_events(ref_entry_id);
"""

# One row per weekly active-learning pass: which label_events it consumed and
# the before/after eval metrics (JSON), so regressions are visible, not silent.
_CREATE_RETRAIN_RUNS = """
CREATE TABLE IF NOT EXISTS retrain_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    events_through_id INTEGER NOT NULL,
    n_events          INTEGER NOT NULL,
    metrics           TEXT NOT NULL DEFAULT '{}'
);
"""

_CREATE_WEIGHT_CACHE = """
CREATE TABLE IF NOT EXISTS weight_cache (
    basis_date TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    figures    TEXT,
    synopsis   TEXT
);
"""

_CREATE_FOOD_SUMMARY = """
CREATE TABLE IF NOT EXISTS food_summary (
    date        TEXT PRIMARY KEY,
    kcal        REAL NOT NULL,
    protein_g   REAL NOT NULL,
    fat_g       REAL NOT NULL,
    carbs_g     REAL NOT NULL,
    entry_count INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, db_path: str):
        self.path = db_path
        self._local = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            # WAL + a generous busy timeout so concurrent writers (bot handlers,
            # scheduled jobs, incoming metrics) wait for the lock instead of
            # failing with "database is locked" and silently dropping the write.
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def _init(self):
        conn = self._conn()
        conn.executescript(_CREATE_ENTRIES)
        conn.executescript(_CREATE_METRICS)
        conn.executescript(_CREATE_REMINDERS)
        conn.executescript(_CREATE_LABEL_EVENTS)
        conn.executescript(_CREATE_RETRAIN_RUNS)
        conn.executescript(_CREATE_WEIGHT_CACHE)
        conn.executescript(_CREATE_FOOD_SUMMARY)
        conn.commit()

    # --- Weight cache ---
    # Both the computed figures and the LLM synopsis are cached keyed on the latest
    # weigh-in date they were derived from: reused until a new weight is logged, then
    # recomputed. So nothing is recalculated on repeat /weight calls or digest runs, and
    # the cache invalidates itself the moment the underlying data changes.

    def max_weight_date(self) -> str | None:
        """The most recent date with a weight reading (cheap, index-backed)."""
        rows = self.query("SELECT MAX(date) AS d FROM metrics WHERE key = 'weight'")
        return rows[0]["d"] if rows and rows[0]["d"] else None

    def weight_cache_get(self, basis_date: str) -> sqlite3.Row | None:
        rows = self.query(
            "SELECT figures, synopsis FROM weight_cache WHERE basis_date = ?",
            (basis_date,),
        )
        return rows[0] if rows else None

    def cache_weight_figures(self, basis_date: str, ts: str, figures: str) -> None:
        self.execute(
            "INSERT INTO weight_cache (basis_date, ts, figures) VALUES (?, ?, ?) "
            "ON CONFLICT(basis_date) DO UPDATE SET ts = excluded.ts, figures = excluded.figures",
            (basis_date, ts, figures),
        )

    def cache_weight_synopsis(self, basis_date: str, ts: str, synopsis: str) -> None:
        self.execute(
            "INSERT INTO weight_cache (basis_date, ts, synopsis) VALUES (?, ?, ?) "
            "ON CONFLICT(basis_date) DO UPDATE SET ts = excluded.ts, synopsis = excluded.synopsis",
            (basis_date, ts, synopsis),
        )

    def latest_weight_synopsis(self) -> str | None:
        rows = self.query(
            "SELECT synopsis FROM weight_cache WHERE synopsis IS NOT NULL "
            "ORDER BY basis_date DESC LIMIT 1"
        )
        return rows[0]["synopsis"] if rows else None

    # --- Food summary ---
    # One row per day: end-of-day macro totals derived from food entries. Written
    # by the daily digest job so the data persists independently of the raw entries.

    def upsert_food_summary(
        self,
        date_str: str,
        kcal: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        entry_count: int,
    ) -> None:
        self.execute(
            """INSERT INTO food_summary (date, kcal, protein_g, fat_g, carbs_g, entry_count)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   kcal        = excluded.kcal,
                   protein_g   = excluded.protein_g,
                   fat_g       = excluded.fat_g,
                   carbs_g     = excluded.carbs_g,
                   entry_count = excluded.entry_count""",
            (date_str, kcal, protein_g, fat_g, carbs_g, entry_count),
        )

    def food_summary_for_range(self, start: date, end: date) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM food_summary WHERE date >= ? AND date <= ? ORDER BY date",
            (start.isoformat(), end.isoformat()),
        )

    # --- Plugin-owned schema ---
    # Core doesn't know what plugins exist, so it can't own their tables. A plugin
    # creates and manages its own table(s) through this generic surface; the table
    # is guaranteed present exactly when its plugin is active.

    def ensure_schema(self, ddl: str) -> None:
        """Create a plugin's table(s) idempotently (`CREATE TABLE IF NOT EXISTS …`)."""
        conn = self._conn()
        conn.executescript(ddl)
        conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Run a write (INSERT/UPDATE/DELETE) and commit."""
        conn = self._conn()
        conn.execute(sql, params)
        conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a read and return all rows."""
        return self._conn().execute(sql, params).fetchall()

    def delete_entry(self, entry_id: int) -> None:
        self.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    def update_entry_tag(self, entry_id: int, tag: str) -> None:
        self.execute("UPDATE entries SET tag = ? WHERE id = ?", (tag, entry_id))

    def insert_entry(self, ts: str, date_str: str, tag: str, content: str) -> int:
        cur = self._conn().execute(
            "INSERT INTO entries (ts, date, tag, content) VALUES (?, ?, ?, ?)",
            (ts, date_str, tag, content),
        )
        self._conn().commit()
        return cur.lastrowid

    def entry_by_id(self, entry_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM entries WHERE id = ?", (entry_id,))
        return rows[0] if rows else None

    def update_entry_content(self, entry_id: int, content: str) -> None:
        self.execute(
            "UPDATE entries SET content = ? WHERE id = ?", (content, entry_id)
        )

    def latest_entry(self, exclude_tags: tuple[str, ...] = ()) -> sqlite3.Row | None:
        """The most recent entry, skipping the given tags — used by /fix to find
        the last message that actually went through classification."""
        placeholders = ",".join("?" * len(exclude_tags))
        where = f"WHERE tag NOT IN ({placeholders})" if exclude_tags else ""
        rows = self.query(
            f"SELECT * FROM entries {where} ORDER BY id DESC LIMIT 1", exclude_tags
        )
        return rows[0] if rows else None

    # --- Label events (append-only classifier corrections/confirmations) ---

    def insert_label_event(
        self,
        ts: str,
        ref_entry_id: int,
        event_type: str,
        from_label: str,
        to_label: str,
        source: str = "user_tap",
    ) -> int:
        cur = self._conn().execute(
            "INSERT INTO label_events (ts, ref_entry_id, event_type, from_label, to_label, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, ref_entry_id, event_type, from_label, to_label, source),
        )
        self._conn().commit()
        return cur.lastrowid

    def label_events_after(self, after_id: int) -> list[sqlite3.Row]:
        """Label events newer than `after_id`, oldest first — the retrain loop's feed."""
        return self.query(
            "SELECT * FROM label_events WHERE id > ? ORDER BY id", (after_id,)
        )

    # --- Retrain runs (weekly active-learning bookkeeping) ---

    def last_retrain_event_id(self) -> int:
        """The last label_events id a retrain run consumed (0 if never run)."""
        rows = self.query("SELECT MAX(events_through_id) AS m FROM retrain_runs")
        return rows[0]["m"] or 0

    def record_retrain_run(
        self, ts: str, events_through_id: int, n_events: int, metrics_json: str
    ) -> None:
        self.execute(
            "INSERT INTO retrain_runs (ts, events_through_id, n_events, metrics) "
            "VALUES (?, ?, ?, ?)",
            (ts, events_through_id, n_events, metrics_json),
        )

    def entries_by_tag(self, tag: str) -> list[sqlite3.Row]:
        """All entries with a given tag, chronological — for reviewing evolution over time."""
        return (
            self._conn()
            .execute(
                "SELECT * FROM entries WHERE tag = ? ORDER BY ts",
                (tag,),
            )
            .fetchall()
        )

    def insert_metric(
        self, ts: str, date_str: str, key: str, value: str, unit: str = ""
    ):
        self._conn().execute(
            "INSERT INTO metrics (ts, date, key, value, unit) VALUES (?, ?, ?, ?, ?)",
            (ts, date_str, key, value, unit),
        )
        self._conn().commit()

    def entries_for_date(self, d: date) -> list[sqlite3.Row]:
        return (
            self._conn()
            .execute(
                "SELECT * FROM entries WHERE date = ? ORDER BY ts",
                (d.isoformat(),),
            )
            .fetchall()
        )

    def entries_for_range(self, start: date, end: date) -> list[sqlite3.Row]:
        return (
            self._conn()
            .execute(
                "SELECT * FROM entries WHERE date >= ? AND date <= ? ORDER BY date, ts",
                (start.isoformat(), end.isoformat()),
            )
            .fetchall()
        )

    def metrics_for_range(self, start: date, end: date) -> list[sqlite3.Row]:
        return (
            self._conn()
            .execute(
                "SELECT * FROM metrics WHERE date >= ? AND date <= ? ORDER BY date, ts",
                (start.isoformat(), end.isoformat()),
            )
            .fetchall()
        )

    def metrics_max_per_day(
        self, start: date, end: date, key: str
    ) -> list[sqlite3.Row]:
        """Return the highest numeric value per day for a given metric key.

        Used for step counts where multiple readings per day exist and the
        highest value is the most complete (end-of-day total wins).
        """
        return (
            self._conn()
            .execute(
                """
            SELECT date, key, CAST(MAX(CAST(value AS REAL)) AS INTEGER) as value, unit
            FROM metrics
            WHERE date >= ? AND date <= ? AND key = ?
            GROUP BY date
            ORDER BY date
            """,
                (start.isoformat(), end.isoformat(), key),
            )
            .fetchall()
        )

    def existing_metric_keys(self) -> set[tuple[str, str]]:
        """Set of (ts, key) already in metrics — used to dedup JSONL recovery."""
        return {
            (r["ts"], r["key"])
            for r in self._conn().execute("SELECT ts, key FROM metrics")
        }

    def existing_entry_keys(self) -> set[tuple[str, str]]:
        """Set of (ts, tag) already in entries — used to dedup JSONL recovery."""
        return {
            (r["ts"], r["tag"])
            for r in self._conn().execute("SELECT ts, tag FROM entries")
        }

    # --- Reminders ---

    def get_reminders(self) -> list[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM reminders ORDER BY rowid").fetchall()

    def add_reminder(self, r: dict) -> str:
        self._conn().execute(
            """INSERT OR REPLACE INTO reminders
               (id, text, type, date, time, day, interval_minutes, window_start, window_end, auto_log)
               VALUES (:id, :text, :type, :date, :time, :day, :interval_minutes, :window_start, :window_end, :auto_log)""",
            {
                "id": r["id"],
                "text": r["text"],
                "type": r["type"],
                "date": r.get("date", ""),
                "time": r.get("time", ""),
                "day": r.get("day"),
                "interval_minutes": r.get("interval_minutes"),
                "window_start": r.get("window_start", "08:00"),
                "window_end": r.get("window_end", "22:00"),
                "auto_log": int(r.get("auto_log", False)),
            },
        )
        self._conn().commit()
        return r["id"]

    def remove_reminder(self, reminder_id: str):
        self._conn().execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        self._conn().commit()

    def save_reminders(self, reminders: list[dict]):
        """Replace all reminders atomically — used by due_now after firing once-reminders."""
        conn = self._conn()
        conn.execute("DELETE FROM reminders")
        for r in reminders:
            conn.execute(
                """INSERT INTO reminders
                   (id, text, type, date, time, day, interval_minutes, window_start, window_end, auto_log)
                   VALUES (:id, :text, :type, :date, :time, :day, :interval_minutes, :window_start, :window_end, :auto_log)""",
                {
                    "id": r["id"],
                    "text": r["text"],
                    "type": r["type"],
                    "date": r.get("date", ""),
                    "time": r.get("time", ""),
                    "day": r.get("day"),
                    "interval_minutes": r.get("interval_minutes"),
                    "window_start": r.get("window_start", "08:00"),
                    "window_end": r.get("window_end", "22:00"),
                    "auto_log": int(r.get("auto_log", False)),
                },
            )
        conn.commit()

    def earliest_entry_date(self) -> date | None:
        row = self._conn().execute("SELECT MIN(date) as d FROM entries").fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def earliest_entry_date_with_tag(self, tag: str) -> date | None:
        row = (
            self._conn()
            .execute("SELECT MIN(date) as d FROM entries WHERE tag = ?", (tag,))
            .fetchone()
        )
        return date.fromisoformat(row["d"]) if row and row["d"] else None

"""
One-time migration: import all existing JSONL log files into SQLite.

Run once from the project root:
  venv/bin/python ops/migrate_to_sqlite.py

Safe to re-run — uses INSERT OR IGNORE with a unique constraint on (ts, tag, content)
so duplicates are skipped rather than doubled.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import Database

LOG_DIR = os.path.join(os.getcwd(), "ops/log")
db = Database(os.path.join(LOG_DIR, "ops.db"))

# Add unique constraint to avoid double-inserts on re-run
conn = db._conn()
conn.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_unique
    ON entries(ts, tag, content)
""")
conn.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_unique
    ON metrics(ts, key, value)
""")
conn.commit()

entries_imported = 0
metrics_imported = 0
skipped = 0

for path in sorted(Path(LOG_DIR).glob("*.jsonl")):
    try:
        d = date.fromisoformat(path.stem)
    except ValueError:
        continue

    date_str = d.isoformat()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            ts = e.get("ts", "")
            tag = e.get("tag", "")
            content = e.get("content", "")

            if tag == "metric":
                key = e.get("key", "")
                value = str(e.get("value", ""))
                unit = e.get("unit", "")
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO metrics (ts, date, key, value, unit) VALUES (?, ?, ?, ?, ?)",
                        (ts, date_str, key, value, unit),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        metrics_imported += 1
                    else:
                        skipped += 1
                except Exception as ex:
                    print(f"  metric error: {ex} — {line[:80]}")
            else:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO entries (ts, date, tag, content) VALUES (?, ?, ?, ?)",
                        (ts, date_str, tag, content),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        entries_imported += 1
                    else:
                        skipped += 1
                except Exception as ex:
                    print(f"  entry error: {ex} — {line[:80]}")

        except json.JSONDecodeError:
            print(f"  bad JSON in {path.name}: {line[:80]}")

conn.commit()

print(f"Done. {entries_imported} entries, {metrics_imported} metrics imported. {skipped} duplicates skipped.")

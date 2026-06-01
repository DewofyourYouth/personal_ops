"""
One-time import of Apple Health weight CSV into SQLite metrics table.

Run once from the project root:
  venv/bin/python ops/import_weight_csv.py /Users/jacobshore/Documents/Weight-2.csv

Safe to re-run — duplicate (ts, key, value) combos are ignored.
"""

import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from db import Database

TZ = ZoneInfo("Asia/Jerusalem")
LOG_DIR = os.path.join(os.getcwd(), "ops/log")
db = Database(os.path.join(LOG_DIR, "ops.db"))

csv_path = sys.argv[1] if len(sys.argv) > 1 else "/Users/jacobshore/Documents/Weight-2.csv"

imported = skipped = 0
conn = db._conn()

with open(csv_path, newline="") as f:
    reader = csv.reader(f)
    next(reader)  # skip "Weight" header
    next(reader)  # skip column names
    for row in reader:
        if len(row) < 2:
            continue
        dt_str, weight_str = row[0].strip(), row[1].strip()
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %I:%M:%S %p")
            dt = dt.replace(tzinfo=TZ)
            ts = dt.isoformat(timespec="seconds")
            date_str = dt.date().isoformat()
            value = str(round(float(weight_str), 1))
        except (ValueError, IndexError):
            continue

        conn.execute(
            "INSERT OR IGNORE INTO metrics (ts, date, key, value, unit) VALUES (?, ?, ?, ?, ?)",
            (ts, date_str, "weight", value, "kg"),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            imported += 1
        else:
            skipped += 1

conn.commit()
print(f"Done. {imported} weight entries imported, {skipped} duplicates skipped.")

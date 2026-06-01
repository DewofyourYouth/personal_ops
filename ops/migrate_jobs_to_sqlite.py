"""
One-time migration: import job_tracker CSV into SQLite job_applications table.

Run once from the project root:
  venv/bin/python ops/migrate_jobs_to_sqlite.py [path/to/applications.csv]

Defaults to ~/development/job_tracker/data/applications.csv.
Safe to re-run — upserts on company+title so duplicates are updated, not doubled.
"""

import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import Database

LOG_DIR = os.path.join(os.getcwd(), "ops/log")
db = Database(os.path.join(LOG_DIR, "ops.db"))

csv_path = sys.argv[1] if len(sys.argv) > 1 else Path.home() / "development/job_tracker/data/applications.csv"

imported = updated = 0
with open(csv_path, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        company      = row.get("Company", "").strip()
        title        = row.get("Job Title", "").strip()
        if not company or not title:
            continue
        url          = row.get("URL", "").strip()
        applied_date = row.get("Applied Date", "").strip()
        status       = row.get("Status", "applied").strip().lower() or "applied"
        notes        = row.get("Notes", "").strip()
        source       = row.get("Source", "").strip()

        job_id = db.upsert_job(company, title, url, applied_date, status, notes, source)
        imported += 1

print(f"Done. {imported} job applications imported/updated.")

# Quick summary
jobs = db.get_jobs()
from collections import Counter
counts = Counter(dict(r)["status"] for r in jobs)
for status, count in sorted(counts.items()):
    print(f"  {status}: {count}")

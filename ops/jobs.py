"""Generate a job-tracking markdown file from the job_tracker applications CSV."""

import csv
import datetime
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# SQLite is the primary store. CSV is legacy / external sync only.
_LOG_DIR = os.path.join(os.getcwd(), "ops/log")

APPLICATIONS_CSV = Path(
    os.environ.get(
        "JOB_TRACKER_CSV",
        Path.home() / "development/job_tracker/data/applications.csv",
    )
)

CONTEXT_DIR = Path(os.environ.get("OPS_CONTEXT_DIR", Path(__file__).parent / "context"))
JOBS_DIR = CONTEXT_DIR / "jobs"


class ApplicationStatus(StrEnum):
    UNKNOWN = "unknown"
    APPLIED = "applied"
    PHONE_SCREEN = "phone_screen"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    WITHDREW = "withdrew"
    OFFER = "offer"


@dataclass
class Application:
    company_name: str
    job_title: str
    url: str
    applied_date: datetime.date | None
    notes: str
    source: str
    status: ApplicationStatus

    def display_name(self) -> str:
        return f"[{self.company_name}]({self.url})" if self.url else self.company_name

    def date_str(self) -> str:
        return self.applied_date.isoformat() if self.applied_date else "—"


def _parse_status(raw: str) -> ApplicationStatus:
    try:
        return ApplicationStatus(raw.strip().lower())
    except ValueError:
        return ApplicationStatus.UNKNOWN


def _parse_date(raw: str) -> datetime.date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def load_applications() -> dict[str, list[Application]]:
    from db import Database
    db = Database(os.path.join(_LOG_DIR, "ops.db"))
    buckets: dict[str, list[Application]] = {s: [] for s in ApplicationStatus}
    for row in db.get_jobs():
        app = Application(
            company_name=row["company"],
            job_title=row["title"],
            url=row["url"] or "",
            applied_date=_parse_date(row["applied_date"]),
            notes=row["notes"] or "",
            source=row["source"] or "",
            status=_parse_status(row["status"]),
        )
        buckets[app.status].append(app)
    return buckets


def _row(*cells: str) -> str:
    return "| " + " | ".join(cells) + " |"


def _bullet_list(apps: list[Application]) -> str:
    if not apps:
        return "_none_\n"
    return "\n".join(f"- {a.display_name()} — {a.job_title}" for a in apps) + "\n"


def render_markdown(buckets: dict[str, list[Application]]) -> str:
    today = datetime.date.today()

    def is_today(a: Application) -> bool:
        return a.applied_date == today

    today_applied      = [a for a in buckets[ApplicationStatus.APPLIED]      if is_today(a)]
    today_phone_screen = [a for a in buckets[ApplicationStatus.PHONE_SCREEN] if is_today(a)]
    today_interview    = [a for a in buckets[ApplicationStatus.INTERVIEW]    if is_today(a)]
    today_rejected     = [a for a in buckets[ApplicationStatus.REJECTED]     if is_today(a)]
    today_offer        = [a for a in buckets[ApplicationStatus.OFFER]        if is_today(a)]
    today_withdrew     = [a for a in buckets[ApplicationStatus.WITHDREW]     if is_today(a)]

    def by_date(apps: list[Application]) -> list[Application]:
        return sorted(apps, key=lambda a: a.applied_date or datetime.date.min, reverse=True)

    lines: list[str] = [
        f"# Job Tracker for {today.isoformat()}\n",
        "## Today\n",
        "### Applied\n",       _bullet_list(today_applied),
        "### Phone Screen\n",  _bullet_list(today_phone_screen),
        "### Interview\n",     _bullet_list(today_interview),
        "### Rejected\n",      _bullet_list(today_rejected),
        "### Offer\n",         _bullet_list(today_offer),
        "### Withdrew\n",      _bullet_list(today_withdrew),
        "## Applied Previously\n",
        _row("Company", "Job Title", "Application Date", "Source", "Notes"),
        _row("-------", "---------", "----------------", "------", "-----"),
    ]
    for a in by_date([a for a in buckets[ApplicationStatus.APPLIED] if not is_today(a)]):
        lines.append(_row(a.display_name(), a.job_title, a.date_str(), a.source, a.notes))

    lines += [
        "",
        "## Phone Screen\n",
        _row("Company", "Job Title", "Date", "Notes"),
        _row("-------", "---------", "----", "-----"),
    ]
    for a in by_date(buckets[ApplicationStatus.PHONE_SCREEN]):
        lines.append(_row(a.display_name(), a.job_title, a.date_str(), a.notes))

    lines += [
        "",
        "## Interviews Waiting For Response\n",
        _row("Company", "Job Title", "Interview Date", "Notes"),
        _row("-------", "---------", "--------------", "-----"),
    ]
    for a in by_date(buckets[ApplicationStatus.INTERVIEW]):
        lines.append(_row(a.display_name(), a.job_title, a.date_str(), a.notes))

    lines += [
        "",
        "## Rejected\n",
        _row("Company", "Job Title", "Rejection Date", "Notes"),
        _row("-------", "---------", "--------------", "-----"),
    ]
    for a in by_date(buckets[ApplicationStatus.REJECTED]):
        lines.append(_row(a.display_name(), a.job_title, a.date_str(), a.notes))

    lines += [
        "",
        "## Withdrew\n",
        _row("Company", "Job Title", "Withdrawal Date", "Notes"),
        _row("-------", "---------", "---------------", "-----"),
    ]
    for a in by_date(buckets[ApplicationStatus.WITHDREW]):
        lines.append(_row(a.display_name(), a.job_title, a.date_str(), a.notes))

    lines += [
        "",
        "## Offer\n",
        _row("Company", "Job Title", "Date Offered", "Notes"),
        _row("-------", "---------", "------------", "-----"),
    ]
    for a in by_date(buckets[ApplicationStatus.OFFER]):
        lines.append(_row(a.display_name(), a.job_title, a.date_str(), a.notes))

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown → CSV (bidirectional sync)
# ---------------------------------------------------------------------------

# Maps the ## section heading (lowercased) to the status it represents
_SECTION_STATUS: dict[str, ApplicationStatus] = {
    "applied previously":              ApplicationStatus.APPLIED,
    "phone screen":                    ApplicationStatus.PHONE_SCREEN,
    "interviews waiting for response": ApplicationStatus.INTERVIEW,
    "rejected":                        ApplicationStatus.REJECTED,
    "withdrew":                        ApplicationStatus.WITHDREW,
    "offer":                           ApplicationStatus.OFFER,
}

_CSV_FIELDNAMES = ["Company", "Job Title", "URL", "Applied Date", "Status", "Notes", "Source"]


def _extract_link(cell: str) -> tuple[str, str]:
    """Return (name, url) from '[name](url)', or (cell, '') if not a link."""
    m = re.match(r"\[([^\]]+)\]\(([^)]*)\)", cell.strip())
    return (m.group(1), m.group(2)) if m else (cell.strip(), "")


def _parse_table_row(line: str) -> list[str] | None:
    """Parse a markdown table row into cells; return None for separator rows."""
    if not line.startswith("|"):
        return None
    cells = [c.strip() for c in line.strip("|").split("|")]
    if all(re.match(r"^-+$", c) for c in cells if c):
        return None  # separator row
    return cells


def parse_jobs_report(path: Path) -> list[Application]:
    """Parse a daily markdown report, returning all applications found in tables."""
    apps: list[Application] = []
    current_status: ApplicationStatus | None = None
    header_seen = False

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()

            if line.startswith("# ") or line.startswith("### "):
                # Top-level title or Today subsections — not a data section
                current_status = None
                header_seen = False
                continue

            if line.startswith("## "):
                section = line[3:].strip().lower()
                current_status = _SECTION_STATUS.get(section)
                header_seen = False
                continue

            if current_status is None:
                continue

            cells = _parse_table_row(line)
            if cells is None:
                continue

            if not header_seen:
                header_seen = True  # first non-separator row is the column header
                continue

            if len(cells) < 2:
                continue

            company, url = _extract_link(cells[0])
            if not company or re.match(r"^-+$", company):
                continue

            job_title = cells[1]
            date_raw = cells[2] if len(cells) > 2 else ""

            if current_status == ApplicationStatus.APPLIED:
                # Company | Job Title | Application Date | Source | Notes
                source = cells[3] if len(cells) > 3 else ""
                notes  = cells[4] if len(cells) > 4 else ""
            else:
                # Company | Job Title | Date | Notes
                source = ""
                notes  = cells[3] if len(cells) > 3 else ""

            apps.append(Application(
                company_name=company,
                job_title=job_title,
                url=url,
                applied_date=_parse_date(date_raw) if date_raw not in ("—", "") else None,
                notes=notes,
                source=source,
                status=current_status,
            ))

    return apps


def sync_to_csv(report_path: Path | None = None) -> tuple[int, int]:
    """Parse the markdown report and write changes back to applications.csv.

    Returns (updated_count, added_count). Only overwrites fields that actually
    changed; notes/URL/date in the CSV are never blanked by an empty markdown cell.
    """
    if report_path is None:
        report_path = JOBS_DIR / f"{datetime.date.today().isoformat()}.md"
    if not report_path.exists():
        return 0, 0

    md_apps = parse_jobs_report(report_path)
    if not md_apps:
        return 0, 0

    # Build lookup keyed by (company_lower, title_lower)
    md_lookup: dict[tuple[str, str], Application] = {
        (a.company_name.lower(), a.job_title.lower()): a
        for a in md_apps
    }

    rows: list[dict] = []
    if APPLICATIONS_CSV.exists():
        with open(APPLICATIONS_CSV, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

    updated = 0
    matched: set[tuple[str, str]] = set()

    for row in rows:
        key = (row.get("Company", "").strip().lower(),
               row.get("Job Title", "").strip().lower())
        if key not in md_lookup:
            continue
        matched.add(key)
        md = md_lookup[key]
        changed = False

        if row.get("Status", "").strip() != str(md.status):
            row["Status"] = str(md.status)
            changed = True

        if md.notes and row.get("Notes", "").strip() != md.notes:
            row["Notes"] = md.notes
            changed = True

        if md.url and not row.get("URL", "").strip():
            row["URL"] = md.url
            changed = True

        if md.applied_date and not row.get("Applied Date", "").strip():
            row["Applied Date"] = md.applied_date.isoformat()
            changed = True

        if changed:
            updated += 1

    # New rows present in markdown but absent from CSV
    added = 0
    for key, md in md_lookup.items():
        if key in matched:
            continue
        rows.append({
            "Company":      md.company_name,
            "Job Title":    md.job_title,
            "URL":          md.url,
            "Applied Date": md.applied_date.isoformat() if md.applied_date else "",
            "Status":       str(md.status),
            "Notes":        md.notes,
            "Source":       md.source,
        })
        added += 1

    if updated or added:
        with open(APPLICATIONS_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)

    return updated, added


def generate_jobs_report() -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    out = JOBS_DIR / f"{datetime.date.today().isoformat()}.md"

    # Sync edits from any existing version of today's file before overwriting
    if out.exists():
        updated, added = sync_to_csv(out)
        if updated or added:
            print(f"  Synced from markdown: {updated} updated, {added} added → {APPLICATIONS_CSV.name}")

    buckets = load_applications()
    out.write_text(render_markdown(buckets), encoding="utf-8")
    return out


def status_summary() -> str:
    from collections import Counter
    buckets = load_applications()
    total = sum(len(v) for v in buckets.values())
    if total == 0:
        return "No applications on record."

    counts = {s: len(buckets[s]) for s in ApplicationStatus}
    today = datetime.date.today()

    lines = [f"📊 <b>Job Search — {total} applications</b>\n"]

    icons = {
        ApplicationStatus.APPLIED:      "📤",
        ApplicationStatus.PHONE_SCREEN: "📞",
        ApplicationStatus.INTERVIEW:    "🔄",
        ApplicationStatus.OFFER:        "🎉",
        ApplicationStatus.REJECTED:     "❌",
        ApplicationStatus.WITHDREW:     "↩️",
        ApplicationStatus.UNKNOWN:      "❓",
    }
    order = [ApplicationStatus.INTERVIEW, ApplicationStatus.PHONE_SCREEN,
             ApplicationStatus.OFFER, ApplicationStatus.APPLIED,
             ApplicationStatus.REJECTED, ApplicationStatus.WITHDREW]
    for s in order:
        if counts[s]:
            lines.append(f"{icons[s]} {s.value.replace('_', ' ').title()}: {counts[s]}")

    # Recent 5
    all_apps = [a for apps in buckets.values() for a in apps]
    recent = sorted([a for a in all_apps if a.applied_date],
                    key=lambda a: a.applied_date, reverse=True)[:5]
    if recent:
        lines.append("\n<b>Recent:</b>")
        for a in recent:
            lines.append(f"{icons[a.status]} {a.company_name} — {a.job_title} ({a.date_str()})")

    # Follow-up candidates
    needs_followup = [a for a in buckets[ApplicationStatus.APPLIED]
                      if a.applied_date and (today - a.applied_date).days >= 7]
    if needs_followup:
        lines.append(f"\n⏰ <b>{len(needs_followup)} applied 7+ days ago</b> — worth following up")

    return "\n".join(lines)


def add_application(company: str, title: str = "", url: str = "",
                    source: str = "", notes: str = "",
                    status: str = "applied", applied_date: str = "") -> Application:
    """Append a new application row to the CSV and return the Application object."""
    parsed_status = _parse_status(status)
    parsed_date = _parse_date(applied_date) or datetime.date.today()
    row = {
        "Company": company.strip(),
        "Job Title": title.strip(),
        "URL": url.strip(),
        "Applied Date": parsed_date.isoformat(),
        "Status": parsed_status,
        "Notes": notes.strip(),
        "Source": source.strip(),
    }
    # Primary: write to SQLite
    from db import Database
    db = Database(os.path.join(_LOG_DIR, "ops.db"))
    db.upsert_job(row["Company"], row["Job Title"], row["URL"],
                  row["Applied Date"], str(parsed_status), row["Notes"], row["Source"])
    return Application(
        company_name=row["Company"],
        job_title=row["Job Title"],
        url=row["URL"],
        applied_date=parsed_date,
        notes=row["Notes"],
        source=row["Source"],
        status=parsed_status,
    )


if __name__ == "__main__":
    import sys
    if "--sync" in sys.argv:
        # Explicit sync-only mode: python ops/jobs.py --sync [path/to/report.md]
        path_arg = next((Path(a) for a in sys.argv[1:] if not a.startswith("-")), None)
        updated, added = sync_to_csv(path_arg)
        print(f"Synced: {updated} updated, {added} added")
    else:
        out = generate_jobs_report()
        print(f"Written to {out}")

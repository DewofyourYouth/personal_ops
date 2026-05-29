"""Generate a job-tracking markdown file from the job_tracker applications CSV."""

import csv
import datetime
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

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
    buckets: dict[str, list[Application]] = {s: [] for s in ApplicationStatus}
    with open(APPLICATIONS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            app = Application(
                company_name=row.get("Company", "").strip(),
                job_title=row.get("Job Title", "").strip(),
                url=row.get("URL", "").strip(),
                applied_date=_parse_date(row.get("Applied Date", "")),
                notes=row.get("Notes", "").strip(),
                source=row.get("Source", "").strip(),
                status=_parse_status(row.get("Status", "")),
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


def generate_jobs_report() -> Path:
    buckets = load_applications()
    md = render_markdown(buckets)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    out = JOBS_DIR / f"{datetime.date.today().isoformat()}.md"
    out.write_text(md, encoding="utf-8")
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


def add_application(company: str, title: str, url: str = "",
                    source: str = "", notes: str = "") -> Application:
    """Append a new application row to the CSV and return the Application object."""
    today = datetime.date.today()
    row = {
        "Company": company.strip(),
        "Job Title": title.strip(),
        "URL": url.strip(),
        "Applied Date": today.isoformat(),
        "Status": ApplicationStatus.APPLIED,
        "Notes": notes.strip(),
        "Source": source.strip(),
    }
    # Append to CSV
    write_header = not APPLICATIONS_CSV.exists()
    with open(APPLICATIONS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Company", "Job Title", "URL",
                                               "Applied Date", "Status", "Notes", "Source"])
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return Application(
        company_name=row["Company"],
        job_title=row["Job Title"],
        url=row["URL"],
        applied_date=today,
        notes=row["Notes"],
        source=row["Source"],
        status=ApplicationStatus.APPLIED,
    )


if __name__ == "__main__":
    out = generate_jobs_report()
    print(f"Written to {out}")

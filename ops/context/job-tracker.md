# Job Tracker

## What It Is

A separate local tool Jacob uses as a career coach console when actively job searching. It handles the full job search workflow: discovering and scoring listings, generating tailored CVs and cover letters, tracking application status, and analyzing search performance.

## How It Connects to This Bot

`ops/jobs.py` reads job_tracker's `data/applications.csv` and generates a daily markdown summary to `ops/context/jobs/YYYY-MM-DD.md`. That file is this bot's window into the job search — a low-resolution snapshot for life-ops awareness.

**The right resolution for this bot:** "Job search is active — N applications sent, M interviews in progress, applied to X this week, pace is on/off track." Not company-level detail or fit analysis — that lives in job_tracker.

## Current State

See today's file in `ops/context/jobs/` for live status. Key things to track at the life-ops level:

- Is the search active? (applications going out weekly?)
- What stage is the funnel at? (mostly applied / interviews / offers?)
- Is the pace meeting the goal? (target: 2+ meaningful applications or recruiter contacts per job search day)
- Any offers or decisions pending?

## Generating / Refreshing the Summary

```bash
cd ~/development/personal_ops && venv/bin/python ops/jobs.py
```

This syncs any edits made to today's markdown back to the CSV, then regenerates the file.

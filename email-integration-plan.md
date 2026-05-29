# Email Integration Plan — Job Application Tracking

## Goal

Scan three email accounts for job-related messages, use an LLM to parse them,
and auto-update `data/applications.csv` in the job_tracker repo with new
status changes and notes. Run on-demand (manually or as part of `ops/jobs.py`).

---

## Accounts

| Account | Provider | Auth method |
| ------- | -------- | ----------- |
| jacobshore@gmail.com | Gmail (personal) | Google OAuth 2.0 |
| jacob@pangolin.dev | Gmail Workspace | Google OAuth 2.0 (separate credential) |
| jacob@dewofyouryouth.com | Proton Mail | Proton Bridge (IMAP on localhost) |

---

## Architecture

```
ops/email_jobs.py
  └── fetch_job_emails()          # pulls raw emails from all accounts
        ├── GmailFetcher          # uses Gmail API (already partially wired in personal_ops)
        └── ImapFetcher           # used for Proton via Bridge
  └── classify_emails()           # LLM call: is this job-related? what changed?
  └── update_applications_csv()   # writes back to job_tracker/data/applications.csv
  └── sync_job_emails()           # top-level: fetch → classify → update
```

Called from `ops/jobs.py::generate_jobs_report()` or standalone:

```bash
venv/bin/python ops/email_jobs.py
```

---

## Step 1 — Gmail (jacobshore@gmail.com)

### Credentials

personal_ops already has `credentials.json` and `token.json` for Google
Calendar. Gmail access needs an additional scope added to the OAuth consent.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → the
   existing project → **APIs & Services → OAuth consent screen**
2. Add scope: `https://www.googleapis.com/auth/gmail.readonly`
3. Delete `token.json` and re-run the auth flow to get a new token with the
   Gmail scope included
4. Add to `.env`:
   ```
   GMAIL_PERSONAL=jacobshore@gmail.com
   ```

### Implementation notes

- Use `googleapiclient.discovery.build("gmail", "v1")` with the existing
  credentials flow in `ops/gcal.py` as a reference
- Query: `label:inbox newer_than:30d` filtered by job-related keywords
  (see §5 below)
- Fetch message headers + plain-text body (no need to decode HTML parts)
- Track processed message IDs in a small state file
  (`ops/context/email_job_state.json`) to avoid re-processing

---

## Step 2 — Gmail Workspace (jacob@pangolin.dev)

Requires a **separate** OAuth 2.0 client credential because it's a different
Google account (even though it's also Gmail).

Options (pick one):

**Option A — Second OAuth client in the same GCP project**

1. Create a second OAuth 2.0 Desktop client in the same project
2. Download as `credentials_pangolin.json`
3. First run: authorize and save token to `token_pangolin.json`
4. Add to `.env`:
   ```
   GMAIL_WORKSPACE=jacob@pangolin.dev
   GMAIL_WORKSPACE_CREDENTIALS=credentials_pangolin.json
   GMAIL_WORKSPACE_TOKEN=token_pangolin.json
   ```

**Option B — Google Workspace service account with domain-wide delegation**

Only viable if you control the Workspace admin for pangolin.dev. More complex
to set up but avoids interactive auth.

Recommendation: **Option A** — simpler, same pattern as personal Gmail.

---

## Step 3 — Proton Mail (jacob@dewofyouryouth.com)

Proton Mail does not support standard IMAP directly. Access requires
**Proton Mail Bridge**, which exposes a local IMAP server.

### Setup

1. Install [Proton Mail Bridge](https://proton.me/mail/bridge) (desktop app)
2. Sign in and enable Bridge for `jacob@dewofyouryouth.com`
3. Bridge runs on `127.0.0.1:1143` (IMAP) by default; note the generated
   Bridge password (different from your Proton login password)
4. Add to `.env`:
   ```
   PROTON_IMAP_HOST=127.0.0.1
   PROTON_IMAP_PORT=1143
   PROTON_IMAP_USER=jacob@dewofyouryouth.com
   PROTON_IMAP_PASSWORD=<bridge-password>
   ```

### Implementation notes

- Use Python's built-in `imaplib` + `email` stdlib — no extra dependencies
- Search criteria: `SINCE 30-days-ago SUBJECT "application"` etc.
- Bridge must be running locally for the sync to work — the script should
  fail gracefully if it can't connect rather than crashing

---

## Step 4 — LLM Classification & CSV Update

### Email classification prompt

For each candidate email, send to the LLM (Anthropic, already in `.env`):

**System:** You are parsing job application emails for a candidate.

**User:**
```
From: {sender}
Subject: {subject}
Date: {date}
Body: {body[:2000]}

Known applications:
{csv_summary}   ← company names + job titles only, no PII

Task:
1. Is this email related to a job application? (yes/no)
2. If yes:
   - Which company / job title does it match (or is it new)?
   - What status does it imply: applied | phone_screen | interview | rejected | offer | withdrew
   - Extract a one-sentence note (e.g. "Interview scheduled for June 3 at 14:00")
   - Is this a duplicate of something already in the CSV?

Return JSON: { "job_related": bool, "company": str, "job_title": str,
               "status": str, "note": str, "is_duplicate": bool }
```

### CSV update logic

```
for each classified email:
  if not job_related → skip
  find matching row in applications.csv by company name (fuzzy)
  if match found:
    update Status if new status is "further along" than current
    append note to Notes field (prepend date)
  else:
    add new row with status = "applied" (or whatever was inferred)
write applications.csv
```

Status progression order (only advance, never regress automatically):
`applied → phone_screen → interview → offer`
`rejected` and `withdrew` can overwrite any status.

---

## Step 5 — Email Filtering (pre-LLM)

Run cheap keyword pre-filter before sending to LLM to reduce API cost:

**Subject keywords (any match → candidate for LLM):**
- application, applied, interview, your candidacy, position, opportunity,
  offer, rejection, unfortunately, next steps, hiring, recruiter, follow up,
  take-home, assessment, coding challenge, scheduling

**Sender domain patterns (likely job-related):**
- greenhouse.io, lever.co, ashbyhq.com, workday.com, smartrecruiters.com,
  taleo.net, icims.com, jobvite.com, linkedin.com, indeed.com, myworkdayjobs.com

**Skip automatically:**
- Newsletters / marketing (`unsubscribe` in body)
- Emails you sent (from == your address)
- Already-processed message IDs (state file)

---

## Step 6 — State Tracking

`ops/context/email_job_state.json`:
```json
{
  "processed_ids": {
    "jacobshore@gmail.com": ["msg_id_1", "msg_id_2"],
    "jacob@pangolin.dev": ["msg_id_3"],
    "jacob@dewofyouryouth.com": ["uid_1"]
  },
  "last_sync": "2026-05-29T10:00:00Z"
}
```

This prevents re-processing old emails on every run.

---

## File Layout

```
personal_ops/
  ops/
    email_jobs.py            ← new: main sync module
    gcal.py                  ← reference for Google auth pattern
  credentials.json           ← existing (personal Gmail + Calendar)
  credentials_pangolin.json  ← new: Workspace Gmail
  token.json                 ← existing (will need Gmail scope added)
  token_pangolin.json        ← new: Workspace Gmail token
  .env                       ← add: GMAIL_PERSONAL, GMAIL_WORKSPACE,
                                PROTON_IMAP_*, GMAIL_WORKSPACE_CREDENTIALS,
                                GMAIL_WORKSPACE_TOKEN
  email-integration-plan.md  ← this file
```

---

## Dependencies to Add (requirements.txt)

No new packages needed:
- Gmail API: already covered by `google-api-python-client` + `google-auth-oauthlib`
- Proton/IMAP: `imaplib` + `email` are Python stdlib
- LLM: `anthropic` already in requirements

---

## Implementation Order

1. **Gmail personal** — lowest friction, auth already partially wired
2. **Proton Bridge** — requires manual Bridge setup but then simple IMAP
3. **Gmail Workspace** — same as personal but separate credential file
4. **LLM classification** — once both fetchers work
5. **CSV update** — final step, wire into `generate_jobs_report()`

---

## Open Questions

- Should `phone_screen` be a status that can come from email? (Recruiters
  often call rather than email for first screens — may need manual entry.)
- What to do with emails for jobs not in the CSV at all — add automatically
  or flag for review?
- Proton Bridge must be running locally: acceptable constraint, or should
  Proton be accessed another way (e.g. export/forward)?

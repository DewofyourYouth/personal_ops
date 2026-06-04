# VPS Migration Plan

## Status
- [x] SQLite migration (done 2026-06-01)
- [x] APScheduler (done 2026-06-01)
- [ ] Docker build verified locally
- [ ] VPS provisioned
- [ ] Deployed and running on VPS
- [ ] Public repo
- [ ] Hume AI voice affect
- [ ] Public dashboard

---

## Step 1: Containerize (local)

- [ ] Start Docker Desktop
- [ ] `docker build -t personal-ops .` — verify it builds clean
- [ ] `docker compose up` — verify it starts and responds in Telegram
- [ ] Stop the test container

---

## Step 2: VPS setup

**Provision:**
- [ ] Choose VPS provider (Hetzner recommended — cheap, good)
- [ ] Install Docker on the VPS
- [ ] Clone personal_ops repo
- [ ] Clone personal-ops-context repo (separately — stays private)

**Secrets and credentials:**
- [ ] Create `.env` with all secrets:
  - `OPS_BOT_TOKEN`
  - `OPS_CHAT_ID`
  - `ANTHROPIC_API_KEY`
  - `OPENAI_API_KEY`
  - `OPS_CONTEXT_DIR` (absolute path to personal-ops-context repo on VPS)
  - `OPS_JOBS_CSV` (path to job tracker CSV on VPS)
- [ ] Copy `credentials.json` and `token.json` (Google Calendar OAuth) to VPS

**Transfer data:**
- [ ] `rsync ops/log/ops.db` to VPS — all log + metric history
- [ ] `rsync ops/log/scheduler.db` to VPS — APScheduler job store
- [ ] Update `docker-compose.yml` context volume mount from `./ops/context` to the absolute path of the personal-ops-context repo on VPS

**Deploy:**
- [ ] `docker compose up -d` on VPS
- [ ] Verify bot responds in Telegram
- [ ] **Stop Mac bot** — only ONE Telegram instance can run at a time

**Context repo sync:**
- [ ] Set up a systemd timer or cron to `git pull` the personal-ops-context repo periodically so Obsidian edits sync through

**Calendar:**
- [ ] Add multi-calendar support: set `GOOGLE_CALENDAR_IDS=jacobshore@gmail.com,jacob@pangolin.dev`
- [ ] Share both calendars with the service account
- [ ] Update `gcal.py` to fetch and merge events from multiple calendar IDs

---

## Post-migration #1 (do FIRST): metrics ingestion endpoint

The iPhone Shortcut has silently dropped a step reading **four days running** (6/2, 6/3,
6/4 all needed manual DB backfill) — because it POSTs to the Telegram Bot API, so the bot is
messaging itself and never sees it (`getUpdates` excludes a bot's own messages). The fix is
the `POST /metrics` ingestion endpoint: the Shortcut POSTs straight to `ops.db`, bypassing
Telegram. Build it as soon as the bot is up on the VPS — it stops the daily data loss.

Design is done in [DASHBOARD_API_SPEC.md](DASHBOARD_API_SPEC.md); see the **Dashboard / API**
section below for the SQLite/concurrency decisions.
- [ ] `api/main.py` — FastAPI `POST /metrics` (Bearer `INGEST_TOKEN`), reuses `logs.write_metric`
- [ ] Add `fastapi` + `uvicorn` to `requirements.txt`
- [ ] `api` service in `docker-compose.yml` (shares `./ops/log`, localhost port, off unless `INGEST_TOKEN` set)
- [ ] Reverse proxy + TLS so the phone can reach it
- [ ] Repoint the Shortcut at `POST /metrics`; verify a reading lands without Telegram
- [ ] Stop manually backfilling steps/weight

---

## Post-migration: Public repo

Currently private GitHub repo — personal context must stay private.

- [ ] Create a public repo with: bot framework, core modules (baseline tracker, hypothesis evaluation, digest logic), spec docs, plugin architecture doc
- [ ] Strip: `ops/context/`, log data, `.env`, anything identifying
- [ ] Replace context files with templates so someone can clone and configure for themselves
- [ ] Decide framing: personal tool vs. open-source project others can actually use (plugin architecture doc suggests the latter)
- [ ] Stronger portfolio piece with a live VPS deployment to point to

---

## Post-migration: Hume AI voice affect

- Hume AI analyzes voice prosody (pitch, rate, energy) before transcription
- Would give automatic affect signal from voice notes without user doing anything extra
- Currently Whisper transcribes and discards all acoustic affect signal
- Hume runs on the audio before Whisper, returns valence/arousal scores
- Fills the gap when explicit mood/energy logs are sparse

---

## Dashboard / API (FastAPI)

- Small FastAPI app on same VPS, its own container, sharing `ops/log/ops.db` with the bot.
- Target: `dashboard.dewofyouryouth.com` (behind reverse proxy + TLS).
- Concurrent writes (bot + API on one `ops.db`) are safe via the WAL + `busy_timeout`
  change shipped 2026-06-03.

**Datastore: stay on SQLite (decided 2026-06-03).** Single user, one box, ~dozens of
writes/day — SQLite's sweet spot, and backup is "rsync one file." Revisit Postgres only
when a real trigger hits: the dashboard/API moves to a **separate host** from the DB
(SQLite can't go over the network), it becomes **multi-user/productized**, or it's wanted
as an explicit ops-learning piece. The swap is contained — `db.py` is the only SQL layer;
routing it through SQLAlchemy Core later would make SQLite→Postgres a connection-string change.

**Write route — the reason to stand the app up now.** iPhone Shortcut POSTs metrics straight
to the DB, bypassing Telegram entirely (fixes the silent-loss bug where Shortcut→Bot-API
messages are the bot talking to itself and never seen by `getUpdates`). Token auth
(`INGEST_TOKEN`). Full design: [DASHBOARD_API_SPEC.md](DASHBOARD_API_SPEC.md).
- `POST /metrics` — weight/steps via `logs.write_metric`.

- [ ] `api/main.py` — FastAPI app with `POST /metrics`, token auth, reuses `Logs`
- [ ] Add `fastapi` + `uvicorn` to `requirements.txt`
- [ ] `api` service in `docker-compose.yml` (shares `./ops/log`, localhost port, off unless `INGEST_TOKEN` set)
- [ ] Point an iPhone Shortcut at it; verify a reading lands in `ops.db`
- [ ] On VPS: reverse proxy + TLS at the dashboard domain

**Jobs retired from personal_ops (decided 2026-06-03).** Job tracking is being handed off to
a dedicated job-application agent (HyperAgent/Hermit). No `POST /jobs`, and the job funnel
will not appear in digests/advisory. Removal is **staged**: decision + endpoint plan dropped
now; the bot's git-push-to-job_tracker coupling removed; full jobs code + `job_applications`
table removal happens once the agent is live. Data exported to
`ops/log/job_applications_export_*.json` (also still in the job_tracker repo).

**Dashboard read routes (later):** habit streak graphs, productivity patterns, weekly
digest summaries — all anonymized (no personal details). Purpose: demo for interviews/clients.

---

## Notes

- APScheduler is already in place (replaced python-telegram-bot's built-in JobQueue) — scheduler jobs persist in `ops/log/scheduler.db` and will transfer cleanly
- SQLite (`ops/log/ops.db`) is a single portable file — `rsync` to VPS and done
- Celery + Redis is a possible future upgrade if multi-worker scheduling is ever needed, but APScheduler is sufficient for now
- Job tracker CSV interface needs to be defined before migration — the CSV path on the VPS will differ from the Mac path

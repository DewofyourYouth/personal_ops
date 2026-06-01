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

## Future: Public dashboard

- Small FastAPI app on same VPS
- Target: `dashboard.dewofyouryouth.com`
- Purpose: demo for job interviews and potential clients
- Data to expose: habit streak graphs, productivity patterns, weekly digest summaries, job search stats — all anonymized (no company names, no personal details)

---

## Notes

- APScheduler is already in place (replaced python-telegram-bot's built-in JobQueue) — scheduler jobs persist in `ops/log/scheduler.db` and will transfer cleanly
- SQLite (`ops/log/ops.db`) is a single portable file — `rsync` to VPS and done
- Celery + Redis is a possible future upgrade if multi-worker scheduling is ever needed, but APScheduler is sufficient for now
- Job tracker CSV interface needs to be defined before migration — the CSV path on the VPS will differ from the Mac path

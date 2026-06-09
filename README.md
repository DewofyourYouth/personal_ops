# Personal Ops Bot

A local-first Telegram bot that acts as a personal ops layer — capturing logs, planning the day with AI assistance, managing a Google Calendar, and sending reminders. Designed to integrate with an Obsidian vault.

## What it does

- Proposes a daily agenda each morning using Claude, aware of your goals, priorities, calendar, and recent logs
- Tracks agenda items (done/missed) and carries state across re-plans; calibrates to your real completion rate over time
- Creates and reads Google Calendar events via natural language
- Sets one-time, daily, and interval reminders (with configurable quiet hours)
- Transcribes voice notes via Whisper and processes them through the same pipeline
- Logs structured entries (notes, insights, wins, metrics) to dated JSONL files
- Weekly AI digest every Sunday summarising wins, patterns, and one suggested adjustment
- Editable personal context files (goals, priorities, constraints, projects, principles) that inform all AI suggestions

## Commands

| Command | Description |
|---|---|
| `/plan` | Generate today's agenda (also runs daily at configured hour) |
| `/agenda` | View open items with Done / Missed buttons |
| `/status` | View all items with their current status (done / missed / open) |
| `/events` | Show today's calendar events |
| `/reminders` | List all reminders (tap 🗑 to delete) |
| `/digest` | AI review of the last 7 days (also runs every Sunday at 20:00) |
| `/metrics` | Tracked metrics with trend (last 14 days) |
| `/logs` | View today's log entries |
| `/grocery` | Shared grocery checklist with check-off buttons and copyable text |
| `/context` | View and edit goals, priorities, constraints, projects, principles |
| `/help` | Show all commands |

## Message prefixes

| Prefix | What it does |
|---|---|
| `done <N or name>` | Mark agenda item done |
| `missed <N or name>` | Mark agenda item missed |
| `add: <text>` | Add your own agenda item |
| `edit <N> <new text>` | Edit an agenda item |
| `event: <description>` / `new calendar event: …` / `add to calendar: …` | Create a Google Calendar event |
| `remind me <...>` | Set a reminder (one-time, daily, or every N minutes) |
| `pick up eggs and milk at the grocery` | Add `eggs` and `milk` to the grocery list |
| 🎙 voice note starting with `grocery …` | Itemize the spoken list into the grocery list (falls back to a log if it isn't one) |
| `metric: <key> <value>` | Log a structured metric (e.g. `metric: steps 8000`) |
| `did: <text>` | Log a spontaneous win (tagged `#win`) |
| `note: / insight: / task: / hypothesis: / checkin` | Log a structured entry |
| *(anything else)* | Logged as `#log` |

Log entry format (JSONL):
```json
{"ts": "2026-05-27T09:00:00+03:00", "tag": "note", "content": "..."}
```

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```
OPS_BOT_TOKEN=your_telegram_bot_token
OPS_CHAT_ID=your_telegram_user_id
ANTHROPIC_API_KEY=your_anthropic_api_key
OPENAI_API_KEY=your_openai_api_key
OPS_MODEL=claude-sonnet-4-6            # optional, default shown
OPS_PLAN_HOUR=8                        # optional, default shown
OPS_PLAN_MINUTE=0                      # optional
```

See [MODEL_USAGE.md](MODEL_USAGE.md) for the current model usage audit.

### Google Calendar

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Library → enable **Google Calendar API**
2. APIs & Services → Credentials → Create OAuth client ID → Desktop app → download JSON → save as `credentials.json` in project root
3. APIs & Services → Audience → Publish app (removes test user restriction)
4. On first run the bot opens a browser for OAuth approval and saves `token.json`

## Running

```bash
source venv/bin/activate
python ops/bot.py
```

Run from the project root — log paths are derived from `os.getcwd()`.

## File structure

```
ops/
  bot.py          — main bot process
  planner.py      — Claude API: agenda proposals, digest, event/reminder parsing
  agenda.py       — daily agenda state (JSON sidecar per day)
  logs.py         — JSONL log read/write, metrics, markdown fallback parser
  context.py      — personal context files (goals, priorities, etc.)
  gcal.py         — Google Calendar integration
  reminders.py    — recurring and one-time reminders
  reminders.json  — persisted reminders
  context/        — personal context files read by the planner
    goals.md
    priorities.md
    constraints.md
    projects.md
    principles.md
  log/            — daily logs (gitignored)
    YYYY-MM-DD.jsonl       — structured log entries
    YYYY-MM-DD-agenda.json — agenda state
tests/
  test_agenda.py
  test_context.py
  test_logs.py
  test_reminders.py
  test_status_command.py
```

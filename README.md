# Personal Ops Bot

A local-first Telegram bot that acts as a personal ops layer — capturing logs, planning the day with AI assistance, managing a Google Calendar, and sending reminders. Designed to integrate with an Obsidian vault.

## What it does

- Proposes a daily agenda each morning at 06:00 using Claude, aware of your goals, priorities, calendar, and recent logs
- Tracks agenda items (done/missed) and carries state across re-plans
- Creates and reads Google Calendar events via natural language
- Sets one-time and recurring reminders
- Transcribes voice notes via Whisper and processes them through the same pipeline
- Appends all entries to dated markdown files (`ops/log/YYYY-MM-DD.md`) for Obsidian

## Commands

| Command | Description |
|---|---|
| `/plan` | Generate today's agenda (also runs daily at 06:00) |
| `/agenda` | View open items with Done / Missed buttons |
| `/events` | Show upcoming events for today |
| `/reminders` | List all reminders (tap 🗑 to delete) |
| `/help` | Show all commands |

## Message prefixes

| Prefix | What it does |
|---|---|
| `done <N or name>` | Mark agenda item done |
| `missed <N or name>` | Mark agenda item missed |
| `add: <text>` | Add your own agenda item |
| `edit <N> <new text>` | Edit an agenda item |
| `event: <description>` / `new calendar event: …` / `add to calendar: …` | Create a Google Calendar event |
| `remind me <...>` | Set a reminder (one-time or recurring) |
| `note: / insight: / task: / hypothesis: / checkin` | Log a structured entry |
| *(anything else)* | Logged as `#log` |

Log entry format:
```
## HH:MM #tag
message content
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
OPS_MODEL=claude-haiku-4-5-20251001   # optional, default shown
OPS_PLAN_HOUR=6                        # optional, default shown
OPS_PLAN_MINUTE=0                      # optional
```

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
  planner.py      — Claude API integration (agenda proposals, event/reminder parsing)
  agenda.py       — daily agenda state (JSON sidecar)
  gcal.py         — Google Calendar integration
  reminders.py    — recurring and one-time reminders
  reminders.json  — persisted reminders
  context/        — personal context files read by the planner
    goals.md
    priorities.md
    constraints.md
    projects.md
    principles.md
  log/            — daily markdown logs (gitignored)
```

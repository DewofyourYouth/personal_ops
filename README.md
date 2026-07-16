# Personal Ops Bot

A local-first Telegram bot that acts as a personal ops layer — capturing logs, planning the day with AI assistance, managing a Google Calendar, and sending reminders. Designed to integrate with an Obsidian vault.

## What it does

- Proposes a daily agenda each morning using Claude, aware of your goals, priorities, calendar, and recent logs
- Tracks agenda items (done/missed) and carries state across re-plans; calibrates to your real completion rate over time
- Tracks habits (streaks, chains, implementation intentions, identities) and negative "slips", with habit-stack routines
- Estimates nutrition for meals typed or photographed, and tracks weight progress with a chart
- Creates and reads Google Calendar events via natural language
- Sets one-time, daily, and interval reminders (with configurable quiet hours)
- Transcribes voice notes via Whisper and processes them through the same pipeline
- Logs structured entries (notes, insights, wins, friction, metrics, food, injections) to dated JSONL files
- Classifies un-prefixed messages with a hybrid classifier (local embedding-KNN first, LLM tie-break on weak votes); every classified entry gets Edit/Reclassify buttons, and corrections feed a weekly retrain loop
- Offers one-tap routing when a message classifies as a `#task` (→ today's agenda or backlog) or `#backlog` (→ backlog)
- Daily + weekly AI digests, an insight ledger, and a quantitative log-mining report
- Editable personal context files (goals, priorities, constraints, projects, principles) that inform all AI suggestions

## Commands

`/help` opens a tap-through category menu of everything below. The Telegram "/" menu is
generated from `BOT_COMMANDS` in `ops/bot_constants.py` and pushed via `set_my_commands`
on startup, so it stays in sync with the handlers. Most primary commands have a
single-letter alias (`/p`, `/a`, `/s`, `/h`, `/l`, `/m`, `/w`, `/v`, `/b`, `/r`, `/d`).

**Planning & agenda**

| Command | Description |
|---|---|
| `/plan` | Generate today's agenda (also runs daily at the configured hour) |
| `/agenda` | Open items with ✅ Done / ❌ Missed buttons |
| `/status` | Day snapshot: open habits, agenda, calendar, and a read on how it's going |
| `/queue` | Queued future agenda items |
| `/events` | Today's calendar events |
| `/reminders` | List reminders (tap 🗑 to delete) |
| `/context` | View and edit goals, priorities, constraints, projects, principles |

**Habits & routines**

| Command | Description |
|---|---|
| `/habits` | Daily checklist with streaks 🔥, chain 🟩⬜, ⚠️ flags |
| `/habitcheck` | On-demand end-of-day habit check (also runs nightly) |
| `/addhabit`, `/edithabit`, `/managehabits` | Add / edit / delete or toggle habits |
| `/habitcue` | Set an implementation intention / habit-stack anchor |
| `/habitnote` | Attach a note to a habit (no args shows recent notes) |
| `/identity` | Habits grouped by the identities they vote for |
| `/habitstrategy` | A 4-Laws plan for a habit you keep missing |
| `/weeklyhabits` | Run weekly habit suggestions now (also Sundays 09:00) |
| `/routines`, `/addroutine`, `/routinestep`, `/delroutine` | Habit-stack routines |
| `/slip`, `/slips`, `/addslip`, `/manageslips` | Track negative habits ("slips") |

**Review & tracking**

| Command | Description |
|---|---|
| `/daily` | End-of-day digest (also nightly at 22:30) |
| `/digest` | Weekly AI review (also Sundays at 20:00) |
| `/insights` | Distil recurring insights from your logs |
| `/metrics` | Tracked metrics with trend (last 14 days) |
| `/mine` | Quantitative log-mining report (`/mine advise` adds an AI read; also Sundays) |
| `/weight` | Weight progress (% lost, rate, chart) |
| `/foodlog`, `/undofood` | Today's food log with macro totals / retract (not delete) an entry |
| `/macros week\|month\|quarter\|year` | Rolling macro totals, averages, and foods consumed |
| `/backlog` | Someday items, grouped by domain |
| `/logs` | Today's log entries |
| `/hypotheses` | Open hypotheses and their follow-ups |
| `/directives` | Standing directives you've declared |

**Grocery**

| Command | Description |
|---|---|
| `/grocery` | Shared grocery checklist with check-off buttons |
| `/addgrocery`, `/grocerycopy`, `/cleargrocery` | Add / copy as text / clear the list |

**Capture & utilities**

| Command | Description |
|---|---|
| `/backdate <when> <entry>` | Log an entry for a past day |
| `/fix` | Reclassify the most recent logged entry |
| `/help` | Category menu of everything |

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
| `add X to my agenda` / `put X on the agenda` | Route X onto today's agenda (explicit destination) |
| `food: <what you ate>` / 📷 food photo | Nutrition estimate to confirm, then log |
| `metric: <key> <value>` | Log a structured metric (e.g. `metric: steps 8000`) |
| `slept 7 hours` / `/sleep 7` | Log last night's sleep as a metric |
| `did: <text>` | Log a spontaneous win (tagged `#win`) |
| `friction: <what went badly>` | Log drag, blockers, mistakes (tagged `#friction`; `wrong:` still works as an alias) |
| `habit: <name>` | Log a completed habit |
| `injection: <dose>` | Log a Wegovy injection (`shot:` / `jab:` also work) |
| `skip: <reason>` | Excuse today's habits (`excuse:` / `excused:` also work) |
| `directive: <rule>` | A standing instruction to the app (`policy:` also works; `/directives` lists them) |
| `discrete: <text>` / `private: <text>` | Log without echoing the content back |
| `backlog: <text>` / `someday: <text>` | Add to the backlog |
| `feedback: <idea/question>` | Log it and get Claude's take |
| `note: / insight: / task: / hypothesis: / checkin` | Log a structured entry |
| 📎 upload an HTML/text file | Extract tasks → `/backlog` and insights → log |
| *(anything else)* | Classified into a tag (hybrid embedding+LLM classifier), falling back to `#log`. Every classified entry gets ✏️ Edit / 🏷 Reclassify buttons; entries classified `#task`/`#backlog` also get one-tap routing buttons to the agenda or backlog |

The tag taxonomy (prefixes, classifier definitions, reclassify picker, mining set) has a
single source of truth: `ops/tags.py`.

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
OPS_CLASSIFIER=hybrid                  # optional: hybrid (default) | embedding | llm
OPS_RECLASSIFY_CONF=0.55               # optional: low-confidence picker threshold
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
  bot.py          — main bot process (composition root)
  tags.py         — canonical tag taxonomy: prefixes, classifier enum, picker, mining set
  text_router.py  — message dispatch: prefix rules, classification, food/voice flows
  classifier.py   — local embedding-KNN classifier (hybrid mode's first pass)
  reclassify_handlers.py — Edit/Reclassify buttons, /fix, label_events corrections
  media.py        — sticker delight layer with per-kind cooldowns
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

# Personal Ops Bot

A local-first Telegram bot that acts as a personal ops layer — capturing logs, insights, hypotheses, and check-ins from Telegram and writing them to dated markdown files. Designed to integrate with an Obsidian vault.

## Message prefixes

| Prefix            | Tag           |
| ----------------- | ------------- |
| `insight: …`      | `#insight`    |
| `hypothesis: …`   | `#hypothesis` |
| `task: …`         | `#task`       |
| `note: …`         | `#note`       |
| `checkin …`       | `#checkin`    |
| *(anything else)* | `#log`        |

Each message is appended to `ops/log/YYYY-MM-DD.md`:

```md
## HH:MM #tag
message content
```

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install python-telegram-bot
```

Create a `.env` file:

```
OPS_BOT_TOKEN=your_bot_token
OPS_CHAT_ID=your_telegram_user_id
```

## Running

```bash
source venv/bin/activate
python ops/bot.py
```

Run from the project root — the log path is derived from `os.getcwd()`.

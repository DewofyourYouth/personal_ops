# Personal Ops Bot

A local-first Telegram bot that acts as a personal ops layer — capturing logs, insights,
hypotheses, and check-ins from Telegram and writing them to dated markdown files.
Designed to integrate with an Obsidian vault.

## What this is

A single long-running Python process (`bot.py`) that:

- Listens for Telegram messages from a single authorized user
- Parses message prefixes to categorize entries
- Appends structured markdown entries to a daily log file

This is personal infrastructure, not a product. Simplicity and reliability over features.

## Project structure

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

Python 3.14 via a local `venv/`. Always use `venv/bin/python` and `venv/bin/pip` rather than system Python.

Required env vars (stored in `.env`, gitignored):
- `OPS_BOT_TOKEN` — Telegram bot token
- `OPS_CHAT_ID` — Telegram user ID that the bot will accept messages from

## Running the bot

Run from the project root (the log path is derived from `os.getcwd()`):

```
source venv/bin/activate
python ops/bot.py
```

## Architecture

`ops/bot.py` is a single-file Telegram bot using `python-telegram-bot`. It accepts messages only from the user identified by `OPS_CHAT_ID`.

Each incoming message is parsed for a prefix (e.g. `insight:`, `hypothesis:`, `task:`, `note:`, `checkin`) and mapped to a hashtag. The message is then appended as a timestamped markdown entry to `ops/log/YYYY-MM-DD.md`. The log directory is gitignored.

Log entry format:
```
## HH:MM #tag
message content
```


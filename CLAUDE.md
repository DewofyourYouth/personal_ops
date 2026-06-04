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

## Architecture and design principles

`ops/bot.py` is the entry point for the Telegram bot using `python-telegram-bot`. It accepts messages only from the user identified by `OPS_CHAT_ID`.

Each incoming message is parsed for a prefix (e.g. `insight:`, `hypothesis:`, `task:`, `note:`, `checkin`) and mapped to a hashtag. The message is then appended as a timestamped markdown entry to `ops/log/YYYY-MM-DD.md`. The log directory is gitignored.

The code is organized into layers, kept deliberately small:

- **Entry point** — `ops/bot.py` is the composition root: it builds the `python-telegram-bot` `Application`, constructs the feature classes with the bot + services, lets them register their own handlers, starts the scheduler, and runs. It should stay thin — no model SDKs, no scheduler internals, no prompt text.
- **Cross-cutting layers** — `llm.py` (all `anthropic`/`openai` calls — input interpretation and output generation), `scheduling.py` (the APScheduler instance + the recurring-job schedule), `tg_common.py` (shared Telegram UI helpers), `bot_constants.py` (prefixes, icons, copy).
- **Feature handler classes** — e.g. `AgendaHandlers`: constructed with the bot instance and the domain services it needs; its Telegram handlers are methods; conversation state lives on the instance, not in module globals; it self-registers via `register(app)`. New tracking domains become new feature classes rather than more functions in `bot.py`.
- **Domain services** — `agenda.py`, `logs.py`, `reminders.py`, `context.py`, `planner.py`, `gcal.py`, `backlog.py`, `baseline_tracker.py`, `db.py`: the deterministic core that owns data and storage logic, with no Telegram concerns. This keeps "AI at the edges, deterministic core" honest.

This is an incremental refactor away from an earlier flat layout — some handlers still live in `bot.py` and move out one feature at a time. The aim is small, clearly-bounded modules that are easy to read and change, not maximal abstraction.
We use type hints and docstrings for clarity, but avoid over-engineering or unnecessary abstractions. The focus is on a stable, maintainable personal tool rather than a scalable product.
We try to follow the design principles in object oriented programming and software architecture, but always with the goal of simplicity and reliability for personal use. The code is not optimized for performance or extensibility, but rather for ease of understanding and modification by the user (me) in the future.

We should use judgement when applying principles — for example, we value simplicity and reliability, but not at the cost of readability or maintainability. If a principle conflicts with the goal of a clear and understandable codebase, we should prioritize clarity. The principles are guidelines, not strict rules, and should be applied with common sense and flexibility. The ultimate goal is a personal tool that serves my needs effectively, not a textbook example of software architecture.


Log entry format:
```
## HH:MM #tag
message content
```

## Testing philosophy

Tests should give me confidence that I didn't accidentally break something when modifying the code in the future. They are not about achieving a certain percentage of coverage or testing every single line of code. Instead, they should focus on critical business logic, edge cases, integration flows, and any parts of the code that have caused bugs before or that I find confusing.

When deciding what to test, I should ask myself:

- Is this code critical to the core functionality of the bot?
- Has this code caused bugs in the past or do I find it confusing?
- Is this code likely to change in the future?
- Is this code well-covered by integration tests or is it trivial enough that I can easily verify it manually? that is critical, has caused bugs before, or is likely to change should be tested. Code that is trivial, well-covered by integration tests, or easy to verify manually may not need its own test. The goal is to have tests that give me confidence when modifying the code in the future, not to achieve a certain percentage of coverage.


### We should have tests that cover:

1. Critical business logic (e.g. log entry formatting, prefix parsing)
2. Edge cases (e.g. messages without prefixes, messages from unauthorized users)
3. Integration tests that simulate incoming messages and verify log file outputs
4. Thing that I might break when modifying the code in the future (e.g. date handling, file writing)
5. Tricky parts of the code that are easy to get wrong (e.g. timezone handling, message parsing)
6. Regression tests - if we made a bug - it is obviously possible to make the same bug again. A test that covers a past bug gives confidence that we won't accidentally reintroduce it in the future.
7. Any part of the code that has caused bugs in the past or that I find confusing when reading it.

### We should not have tests that cover:

1. Trivial code that is unlikely to break (e.g. simple getters/setters, basic string formatting)
2. Code that is well-covered by integration tests (e.g. the overall flow of receiving a message and writing to a log file)
3. Code that is unlikely to change or that I can easily verify manually (e.g. the exact format of the log entry, as long as it is consistent and readable)

We should use judgement when deciding what to test - coverage is not important - confidence we didn't accidently break something is. If a piece of code is critical or has caused bugs before, it should be tested. If it's trivial or well-covered by other tests, it may not need its own test. The goal of testing is to give me confidence when modifying the code in the future, not to achieve a certain percentage of coverage.
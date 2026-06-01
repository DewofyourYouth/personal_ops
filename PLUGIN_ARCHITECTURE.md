# Plugin Architecture Plan

> Status: idea capture only. Do not implement until the problem domain is better understood.
> The job tracker integration will likely be the first real signal of where plugin boundaries belong.

## The problem

personal_ops is growing modules (job tracking, health, food, habits, calendar) that each have:
- their own data store
- their own Telegram commands
- their own log tags
- their own context files
- their own digest contributions

Right now these are just files in `ops/`. That's fine for now. But the seams are becoming visible.

## The idea

A lean core with modules that register themselves:

```
core/
  bot.py          — Telegram I/O only
  scheduler.py    — Celery/APScheduler task registry
  logs.py         — base log read/write
  context.py      — context file loading

modules/
  habits/
  food/
  jobs/
  health/
  calendar/
```

Each module exposes:
- `commands` — list of (command_name, handler) to register
- `prefixes` — dict of prefix → tag (e.g. `{"food:": "#food"}`)
- `digest_context(days)` — summary string to include in digests
- `context_files` — list of files to load into the LLM context

The core bot loops over registered modules at startup to wire everything up.

## What this solves

- Adding a new tracking domain doesn't touch bot.py
- Modules can be enabled/disabled per deployment (personal vs. shared)
- Clean interface for the future dashboard API (each module exposes its own read endpoint)
- If personal_ops ever becomes a product, modules become the unit of customisation

## What it doesn't solve yet

- The job tracker CSV path problem (that's an interface problem, not a plugin problem)
- Celery task registration per module (needs more thought)
- How modules share state (e.g. food logs informing the health digest)

## When to revisit

- After the VPS migration is stable
- After the job tracker interface is designed
- After the dashboard API has a first version
- When adding a new module starts feeling painful with the current structure

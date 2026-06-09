# Model Usage

Last audited: 2026-06-09.

## Runtime Configuration

- `ops/config.py` defines the configurable model default as `claude-sonnet-4-6`.
- `ops/config.py` reads `OPS_MODEL`, falling back to `claude-sonnet-4-6`.
- `ops/bot.py` passes `config.model` into `Planner`.
- Any method below listed as `self.model` uses `OPS_MODEL` when set, otherwise `claude-sonnet-4-6`.

## Claude Sonnet Default

These runtime calls use `Planner.self.model`, which is Sonnet by default:

- `ops/planner.py` - `Planner.propose`
- `ops/planner.py` - `Planner.dedupe`
- `ops/planner.py` - `Planner.classify_backlog_domains`
- `ops/planner.py` - `Planner.extract_actions`
- `ops/planner.py` - `Planner.digest`
- `ops/planner.py` - `Planner.daily_digest`
- `ops/planner.py` - `Planner.feedback`
- `ops/planner.py` - `Planner.habit_strategy`
- `ops/planner.py` - `Planner.weight_synopsis`
- `ops/planner.py` - `Planner.extract_insights`
- `ops/planner.py` - `Planner.evaluate_hypothesis`

## Claude Haiku

These runtime calls hardcode `claude-haiku-4-5-20251001`:

- `ops/planner.py` - `Planner.parse_event`
- `ops/planner.py` - `Planner.parse_reminder`
- `ops/planner.py` - `Planner.estimate_food`
- `ops/planner.py` - `Planner.estimate_food_from_image`
- `ops/llm.py` - `parse_queue_entry`
- `ops/habit_handlers.py` - `match_habit`

## OpenAI Whisper

These runtime calls use `whisper-1`:

- `ops/llm.py` - `transcribe`

## Test-Only Models

- `tests/test_planner_dedupe.py` uses `claude-test` as a mocked Planner model.

## No Other Runtime Models Found

No Opus model, GPT chat model, or other model string is used in the runtime code at the time of this audit.

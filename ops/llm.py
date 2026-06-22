"""Transitional home for two model calls whose domain modules don't exist yet.

NOTE: this is NOT meant to be a permanent "all LLM calls live here" layer —
domain-specific model calls belong in their domain (habit matching moved to
habit_handlers). What's left:
  - transcribe(): generic audio→text; the one genuinely-shared utility. Moves to
    a voice module when voice is extracted (or stays a small util).
  - parse_queue_entry(): queue-specific; moves to the queue plugin when extracted.

Kept here only so the entrypoint (bot.py) doesn't import the SDKs directly until
those two domains are carved out.
"""

from datetime import date

import anthropic
import openai


def transcribe(audio_path: str) -> str:
    """Transcribe a voice note via Whisper. Returns the stripped transcript."""
    with open(audio_path, "rb") as audio:
        transcript = openai.OpenAI().audio.transcriptions.create(
            model="whisper-1", file=audio
        )
    return transcript.text.strip()


async def classify_entry(text: str) -> str:
    """Classify a log entry as a tag when no explicit prefix was detected.

    Returns one of: insight, hypothesis, note, task, wrong, win, backlog,
    checkin, values, log (default). Never returns habit/food/injection/skip —
    those require explicit prefixes because they trigger side effects.
    """
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        tools=[
            {
                "name": "classify",
                "description": "Classify a personal log entry into one tag",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tag": {
                            "type": "string",
                            "enum": [
                                "insight",
                                "hypothesis",
                                "note",
                                "task",
                                "wrong",
                                "win",
                                "backlog",
                                "checkin",
                                "values",
                                "log",
                            ],
                        }
                    },
                    "required": ["tag"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "classify"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify this personal log entry as exactly one tag:\n\n"
                    f'"{text}"\n\n'
                    "insight — a new realization, lesson, or pattern noticed\n"
                    "hypothesis — an empirical claim to test (I think X causes Y)\n"
                    "note — a general observation or reference note\n"
                    "task — something to do or action item\n"
                    "wrong — a mistake, problem, or thing that went badly\n"
                    "win — an accomplishment or positive outcome\n"
                    "backlog — a someday/maybe idea, not urgent\n"
                    "checkin — emotional, physical, or energy status update\n"
                    "values — reflection on personal principles or identity\n"
                    "log — anything else (default fallback)"
                ),
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input["tag"]
    return "log"


async def parse_queue_entry(text: str) -> dict | None:
    """Extract {day, item} from a queue/defer request via Claude tool use."""
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=[
            {
                "name": "queue_agenda_item",
                "description": "Extract the target day and agenda item text from a queue/defer request",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "day": {
                            "type": "string",
                            "description": "Day name or date, e.g. 'Sunday', 'Monday', 'tomorrow'",
                        },
                        "item": {
                            "type": "string",
                            "description": "The agenda item text to queue",
                        },
                    },
                    "required": ["day", "item"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "queue_agenda_item"},
        messages=[
            {"role": "user", "content": f"Today is {date.today()}. Parse this: {text}"}
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None

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
        transcript = openai.OpenAI().audio.transcriptions.create(model="whisper-1", file=audio)
    return transcript.text.strip()


async def parse_queue_entry(text: str) -> dict | None:
    """Extract {day, item} from a queue/defer request via Claude tool use."""
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=[{
            "name": "queue_agenda_item",
            "description": "Extract the target day and agenda item text from a queue/defer request",
            "input_schema": {
                "type": "object",
                "properties": {
                    "day":  {"type": "string", "description": "Day name or date, e.g. 'Sunday', 'Monday', 'tomorrow'"},
                    "item": {"type": "string", "description": "The agenda item text to queue"},
                },
                "required": ["day", "item"],
            },
        }],
        tool_choice={"type": "tool", "name": "queue_agenda_item"},
        messages=[{"role": "user", "content": f"Today is {date.today()}. Parse this: {text}"}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None

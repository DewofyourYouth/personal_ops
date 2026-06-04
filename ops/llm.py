"""LLM edge — input interpretation that calls the model SDKs.

Keeps `anthropic`/`openai` out of the Telegram entrypoint (bot.py). This is the
"AI at the edges" boundary: handlers pass plain data in and get plain data out;
the SDK calls live here. (Output generation lives in planner.py — the other
half of this layer; prompt text is slated to move out of inline next.)
"""
from datetime import date

import anthropic
import openai


def transcribe(audio_path: str) -> str:
    """Transcribe a voice note via Whisper. Returns the stripped transcript."""
    with open(audio_path, "rb") as audio:
        transcript = openai.OpenAI().audio.transcriptions.create(model="whisper-1", file=audio)
    return transcript.text.strip()


async def match_habit(text: str, habit_names: list[str]) -> str | None:
    """Pick which of `habit_names` a free-text log entry satisfies, or None.

    Semantic match (e.g. "took a stroll" -> "Daily walk") via the cheapest model,
    constrained to the actual habit names. Replaces the old stopword/word-overlap
    heuristic. Called once at log time; the result is stored so the checklist
    renders deterministically.
    """
    if not habit_names:
        return None
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        tools=[{
            "name": "match_habit",
            "description": "Pick which habit a free-text log entry satisfies, or 'none'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "habit": {
                        "type": "string",
                        "enum": [*habit_names, "none"],
                        "description": "The habit this entry satisfies, or 'none' if it matches no habit.",
                    },
                },
                "required": ["habit"],
            },
        }],
        tool_choice={"type": "tool", "name": "match_habit"},
        messages=[{"role": "user", "content": f"Habits: {habit_names}\nLog entry: {text!r}\nWhich habit does this satisfy?"}],
    )
    for block in response.content:
        if block.type == "tool_use":
            choice = block.input.get("habit")
            return None if choice in (None, "none") else choice
    return None


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

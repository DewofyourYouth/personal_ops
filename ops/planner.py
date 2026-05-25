import re
from datetime import date, timedelta
from pathlib import Path

import anthropic

CONTEXT_DIR = Path(__file__).parent / "context"
CONTEXT_FILES = ["goals.md", "priorities.md", "constraints.md", "projects.md", "principles.md"]


def day_type():
    day = date.today().weekday()
    if day in (0, 2, 4):
        return "Haki development day (Mon/Wed/Fri)"
    elif day in (1, 3):
        return "Job search day (Tue/Thu)"
    else:
        return "Weekend / Shabbat"


def _load_context():
    parts = []
    for fname in CONTEXT_FILES:
        path = CONTEXT_DIR / fname
        if path.exists():
            parts.append(f"### {fname}\n{path.read_text().strip()}")
    return "\n\n".join(parts)


def _load_recent_logs(log_dir, days=3):
    entries = []
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        path = Path(log_dir) / f"{d}.md"
        if path.exists():
            entries.append(f"### {d}\n{path.read_text().strip()}")
    return "\n\n".join(entries) if entries else "No recent logs."


async def propose(model, log_dir, calendar_events="", existing_summary=""):
    client = anthropic.AsyncAnthropic()
    context = _load_context()
    recent = _load_recent_logs(log_dir)

    user_content = f"Today is a {day_type()}.\n\n"
    if calendar_events:
        user_content += f"Today's calendar:\n{calendar_events}\n\n"
    if existing_summary:
        user_content += f"Today's agenda so far:\n{existing_summary}\n\n"
    user_content += f"Recent log entries:\n{recent}\n\nPropose today's agenda."

    response = await client.messages.create(
        model=model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a personal ops assistant. "
                    "Propose a focused, realistic agenda for today based on the user's goals, "
                    "constraints, calendar, and recent activity. "
                    "Schedule around calendar events — don't suggest deep work blocks that overlap with them. "
                    "Each item must be a single, independently completable action — never bundle two distinct activities into one item. "
                    "If today's agenda already has open items, include them in your proposal. Do not re-propose items already marked done or missed. "
                    "Return 3–7 specific, actionable items as a plain numbered list (e.g. '1. Do X'). "
                    "Nothing else — no preamble, no commentary."
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"## User context\n\n{context}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_content}],
    )

    raw = response.content[0].text.strip()
    return [
        re.sub(r"^\d+\.\s*", "", line.strip())
        for line in raw.splitlines()
        if re.match(r"^\d+\.", line.strip())
    ]


async def parse_event(text):
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[{
            "name": "create_calendar_event",
            "description": "Parse a natural language event description into structured fields",
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "HH:MM (24h)"},
                    "duration_minutes": {"type": "integer", "description": "Default 60"},
                    "description": {"type": "string"},
                },
                "required": ["summary", "date", "start_time"],
            },
        }],
        tool_choice={"type": "tool", "name": "create_calendar_event"},
        messages=[{
            "role": "user",
            "content": f"Today is {date.today()} ({day_type()}). Parse this event: {text}",
        }],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None


async def parse_reminder(text):
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[{
            "name": "create_reminder",
            "description": "Parse a natural language reminder into structured fields",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The reminder message"},
                    "type": {"type": "string", "enum": ["daily", "interval"], "description": "'daily' for once per day at a fixed time, 'interval' for repeating every N minutes"},
                    "time": {"type": "string", "description": "HH:MM (24h) — required for daily type"},
                    "interval_minutes": {"type": "integer", "description": "Minutes between reminders — required for interval type"},
                    "window_start": {"type": "string", "description": "HH:MM — start of active window for interval type (default 08:00)"},
                    "window_end": {"type": "string", "description": "HH:MM — end of active window for interval type (default 22:00)"},
                },
                "required": ["text", "type"],
            },
        }],
        tool_choice={"type": "tool", "name": "create_reminder"},
        messages=[{"role": "user", "content": f"Parse this reminder: {text}"}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None

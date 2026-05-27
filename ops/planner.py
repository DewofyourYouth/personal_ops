import json
import re
from collections import defaultdict
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
        path = Path(log_dir) / f"{d}.jsonl"
        if path.exists():
            lines = []
            for line in path.read_text().splitlines():
                try:
                    e = json.loads(line)
                    lines.append(f"[{e['ts']}] #{e['tag']}: {e['content']}")
                except Exception:
                    pass
            if lines:
                entries.append(f"### {d}\n" + "\n".join(lines))
    return "\n\n".join(entries) if entries else "No recent logs."


def _load_completion_history(log_dir, days=14):
    counts: dict = defaultdict(lambda: {"done": 0, "missed": 0, "total": 0})
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        path = Path(log_dir) / f"{d}-agenda.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            for item in data.get("items", []):
                key = item["text"].lower()[:60]
                counts[key]["total"] += 1
                if item["status"] in ("done", "missed"):
                    counts[key][item["status"]] += 1
        except Exception:
            pass

    lines = []
    for text, c in sorted(counts.items(), key=lambda x: -x[1]["total"]):
        if c["total"] >= 2:
            lines.append(f"- \"{text}\": done {c['done']}/{c['total']}, missed {c['missed']}/{c['total']}")
    return ("Completion history (last 14 days):\n" + "\n".join(lines)) if lines else ""


async def propose(model, log_dir, calendar_events="", existing_summary=""):
    client = anthropic.AsyncAnthropic()
    context = _load_context()
    recent = _load_recent_logs(log_dir)
    history = _load_completion_history(log_dir)

    user_content = f"Today is a {day_type()}.\n\n"
    if calendar_events:
        user_content += f"Today's calendar:\n{calendar_events}\n\n"
    if existing_summary:
        user_content += f"Today's agenda so far:\n{existing_summary}\n\n"
    if history:
        user_content += f"{history}\n\n"
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
                    "Use completion history to calibrate: if the user consistently misses an item type, reduce its frequency or reframe it as smaller. "
                    "If they consistently complete something, keep it. Adapt to their real capacity, not their ideal. "
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


async def digest(model, log_dir, days=7):
    client = anthropic.AsyncAnthropic()
    context = _load_context()
    logs = _load_recent_logs(log_dir, days=days)
    history = _load_completion_history(log_dir, days=days)

    user_content = f"Review the last {days} days.\n\n"
    if history:
        user_content += f"{history}\n\n"
    user_content += f"Log entries:\n{logs}"

    response = await client.messages.create(
        model=model,
        max_tokens=700,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a personal ops assistant doing a periodic review. "
                    "Analyze the user's recent logs and agenda completion data. "
                    "Return a digest in exactly this format — no extra text:\n\n"
                    "✅ Wins: (2-3 bullet points of things going well or completed)\n"
                    "⚠️ Patterns to watch: (2-3 recurring issues, missed items, or friction points)\n"
                    "💡 Insight: (1 sentence — the most useful non-obvious observation)\n"
                    "🔧 Suggested adjustment: (1 concrete change to goals, priorities, or habits)\n\n"
                    "Be specific and direct. Reference actual log content. No generic advice."
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
    return response.content[0].text.strip()


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
                    "type": {"type": "string", "enum": ["once", "daily", "interval"], "description": "'once' for a one-time reminder on a specific date/time (default), 'daily' only if user says 'every day' or 'daily', 'interval' for repeating every N minutes"},
                    "date": {"type": "string", "description": "YYYY-MM-DD — required for 'once' type. Resolve relative expressions: 'tomorrow', 'in a week', 'on June 23rd', etc. against today's date."},
                    "time": {"type": "string", "description": "HH:MM (24h) — required for 'once' and 'daily' types"},
                    "interval_minutes": {"type": "integer", "description": "Minutes between reminders — required for interval type"},
                    "window_start": {"type": "string", "description": "HH:MM — start of active window for interval type. Only set if user explicitly says a start time (e.g. 'from 9am'). Do NOT set a default — the system defaults to 08:00."},
                    "window_end": {"type": "string", "description": "HH:MM — end of active window for interval type. Only set if user explicitly says an end time (e.g. 'until 6pm'). Do NOT set a default — the system defaults to 22:00."},
                },
                "required": ["text", "type"],
            },
        }],
        tool_choice={"type": "tool", "name": "create_reminder"},
        messages=[{"role": "user", "content": f"Today is {date.today().isoformat()}. Parse this reminder: {text}"}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None

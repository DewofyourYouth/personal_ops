import json
import re
from datetime import date, timedelta
from pathlib import Path

import anthropic

from context import Context
from logs import Logs


def day_type() -> str:
    day = date.today().weekday()
    if day in (0, 2, 4):
        return "Haki development day (Mon/Wed/Fri)"
    elif day in (1, 3):
        return "Job search day (Tue/Thu)"
    else:
        return "Weekend / Shabbat"


class Planner:
    def __init__(self, model: str, logs: Logs, context: Context | None = None):
        self.model = model
        self.logs = logs
        self.context = context or Context()

    async def propose(self, calendar_events: str = "", existing_summary: str = "") -> list[str]:
        client = anthropic.AsyncAnthropic(max_retries=4)
        history = self._completion_history()

        user_content = f"Today is a {day_type()}.\n\n"
        if calendar_events:
            user_content += f"Today's calendar:\n{calendar_events}\n\n"
        if existing_summary:
            user_content += f"Today's agenda so far:\n{existing_summary}\n\n"
        if history:
            user_content += f"{history}\n\n"
        user_content += f"Recent log entries:\n{self.logs.read_recent(days=3)}\n\nPropose today's agenda."

        response = await client.messages.create(
            model=self.model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": (
                        "You are a personal ops assistant. "
                        "Propose a focused, realistic agenda for today based on the user's goals, constraints, calendar, and recent activity. "
                        "Follow the rules in agenda-rules.md exactly. "
                        "Return a plain numbered list (e.g. '1. Do X'). Nothing else."
                    ),
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"## User context\n\n{self.context.load_all()}",
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

    async def digest(self, days: int = 7) -> str:
        client = anthropic.AsyncAnthropic(max_retries=4)
        history = self._completion_history(days=days)
        metrics_text = self.logs.format_metrics_for_prompt(days=days)

        stats_text = self.logs.format_stats_for_prompt(days=days)

        user_content = f"Review the last {days} days.\n\n"
        if stats_text:
            user_content += f"{stats_text}\n\n"
        if history:
            user_content += f"{history}\n\n"
        if metrics_text:
            user_content += f"{metrics_text}\n\n"
        user_content += f"Log entries:\n{self.logs.read_recent(days=days)}"

        response = await client.messages.create(
            model=self.model,
            max_tokens=700,
            system=[
                {
                    "type": "text",
                    "text": (
                        "You are a personal ops assistant doing a periodic review. "
                        "Follow the review rules, tone, and structure defined in the user context (review-rules.md and bot-personality.md). "
                        "Analyze the user's recent logs and agenda completion data. "
                        "Return a digest in exactly this format — no extra text:\n\n"
                        "✅ Wins: (2-3 bullet points of things going well or completed)\n"
                        "⚠️ Patterns to watch: (2-3 recurring issues, missed items, or friction points — classify the cause, don't just list them)\n"
                        "💡 Insight: (1 sentence — the most useful non-obvious observation)\n"
                        "🔧 Suggested adjustment: (1 concrete change — the smallest correction, not a grand reset)\n\n"
                        "Be specific and direct. Reference actual log content. No generic advice. No shame. No hype.\n\n"
                        "Important caveats:\n"
                        "- If coverage is fewer than 5 days, say so and treat all patterns as tentative. Do not state patterns as established facts.\n"
                        "- If a log entry explicitly states what happened (e.g. 'We learned Yoma every day this week'), treat that as authoritative — it overrides inferences from agenda completion data.\n"
                        "- Early log entries may contain bot-test noise (short fragments, repeated command words). Do not read these as real activity signals.\n"
                        "- A missed agenda item caused by an external constraint (e.g. chavrusa canceled, appointment ran over) is not a behavioral pattern. Classify it correctly.\n"
                        "- Habits (defined in habits.md) are NOT tracked via the agenda. Do not infer whether habits were completed or missed from agenda data. If a habit appears in the agenda history, ignore its completion status — it proves nothing about whether the habit was actually done.\n"
                        "- Habit completion IS tracked via explicit `habit:` log entries. The stats include a Habit log table showing which habits were logged and on how many days. Use this as the authoritative source for habit adherence. Absence from the habit log on a given day means the habit was not logged — not necessarily that it wasn't done."
                    ),
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"## User context\n\n{self.context.load_all()}",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()

    async def daily_digest(self, target_date: date | None = None) -> str:
        client = anthropic.AsyncAnthropic(max_retries=4)
        history = self._completion_history(days=14)
        stats_text = self.logs.format_stats_for_prompt(days=7)

        d = target_date or date.today()
        user_content = f"Date: {d} ({day_type()}).\n\n"
        user_content += f"Log for {d}:\n{self.logs.read_day_as_text(d)}\n\n"
        if stats_text:
            user_content += f"{stats_text}\n\n"
        if history:
            user_content += f"{history}\n\n"
        user_content += "Generate today's end-of-day digest."

        response = await client.messages.create(
            model=self.model,
            max_tokens=600,
            system=[
                {
                    "type": "text",
                    "text": (
                        "You are a personal ops assistant generating an end-of-day digest. "
                        "Review the user's day — their logs, habit tracking, agenda completion, and recent history.\n\n"
                        "Return the digest in exactly this format — no extra text:\n\n"
                        "💬 \"[quote]\" — [Author], [Source with specific reference]\n\n"
                        "✅ Wins\n"
                        "- [2–3 specific things that went well today]\n\n"
                        "⬆️ Improve\n"
                        "- [1–2 things that could have gone better today]\n\n"
                        "👁 Keep An Eye On\n"
                        "- [1–2 patterns or risks worth watching over the coming days]\n\n"
                        "💡 Suggestions\n"
                        "- [1–2 concrete, small adjustments for tomorrow or this week]\n\n"
                        "For the opening quote: choose a short, genuinely relevant passage from either a Stoic thinker "
                        "(Marcus Aurelius, Epictetus, Seneca) or the Talmud. "
                        "For Talmud: include tractate and daf or a named sage. "
                        "The quote must connect to the actual key insight from this specific day — not be generic. "
                        "Be specific and direct throughout. Reference actual log content. No hype. No shame. No generic advice."
                    ),
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"## User context\n\n{self.context.load_all()}",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()

    async def feedback(self, text: str) -> str:
        client = anthropic.AsyncAnthropic(max_retries=4)
        response = await client.messages.create(
            model=self.model,
            max_tokens=400,
            system=[
                {
                    "type": "text",
                    "text": (
                        "You are a personal ops assistant giving feedback on an idea, question, or plan. "
                        "Follow the tone in bot-personality.md: warm, direct, practical. "
                        "Be concise — this is a Telegram message. "
                        "Structure your response as: what's strong, what's weak or worth watching, one concrete next step or question. "
                        "No hype. No generic advice. No long preamble."
                    ),
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"## User context\n\n{self.context.load_all()}",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": text}],
        )
        return response.content[0].text.strip()

    async def parse_event(self, text: str) -> dict | None:
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
                        "summary":          {"type": "string"},
                        "date":             {"type": "string", "description": "YYYY-MM-DD"},
                        "start_time":       {"type": "string", "description": "HH:MM (24h)"},
                        "duration_minutes": {"type": "integer", "description": "Default 60"},
                        "description":      {"type": "string"},
                    },
                    "required": ["summary", "date", "start_time"],
                },
            }],
            tool_choice={"type": "tool", "name": "create_calendar_event"},
            messages=[{"role": "user", "content": f"Today is {date.today()} ({day_type()}). Parse this event: {text}"}],
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        return None

    async def parse_reminder(self, text: str) -> dict | None:
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
                        "text":             {"type": "string", "description": "The reminder message"},
                        "type":             {"type": "string", "enum": ["once", "daily", "interval"],
                                             "description": "'once' for a one-time reminder (default), 'daily' if user says 'every day', 'interval' for repeating every N minutes"},
                        "date":             {"type": "string", "description": "YYYY-MM-DD — required for 'once'. Resolve relative dates ('tomorrow', 'in a week', 'June 23rd') against today."},
                        "time":             {"type": "string", "description": "HH:MM (24h) — required for 'once' and 'daily'"},
                        "interval_minutes": {"type": "integer", "description": "Minutes between reminders — required for 'interval'"},
                        "window_start":     {"type": "string", "description": "HH:MM — only set if user specifies a start time. System default: 08:00."},
                        "window_end":       {"type": "string", "description": "HH:MM — only set if user specifies an end time. System default: 22:00."},
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

    def _completion_history(self, days: int = 14) -> str:
        from collections import defaultdict
        counts: dict = defaultdict(lambda: {"done": 0, "missed": 0, "total": 0})
        for i in range(1, days + 1):
            d = date.today() - timedelta(days=i)
            path = Path(self.logs.log_dir) / f"{d}-agenda.json"
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

        lines = [
            f"- \"{text}\": done {c['done']}/{c['total']}, missed {c['missed']}/{c['total']}"
            for text, c in sorted(counts.items(), key=lambda x: -x[1]["total"])
            if c["total"] >= 2
        ]
        return ("Completion history:\n" + "\n".join(lines)) if lines else ""

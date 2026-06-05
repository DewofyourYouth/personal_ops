import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import anthropic

from context import Context
from logs import Logs
from baseline_tracker import Baseline
from insights import KINDS as INSIGHT_KINDS
from insights import Insights


def _day_type_for(d: date) -> str:
    day = d.weekday()
    if day in (0, 2, 4):
        return "Haki development day (Mon/Wed/Fri)"
    elif day in (1, 3):
        return "Job search day (Tue/Thu)"
    elif day == 6:
        return "Sunday (marketability / independent income day)"
    else:  # Saturday
        return "Shabbat"


def day_type() -> str:
    return _day_type_for(date.today())


class Planner:
    def __init__(self, model: str, logs: Logs, context: Context | None = None):
        self.model = model
        self.logs = logs
        self.context = context or Context()
        self.baseline = Baseline(logs.log_dir)
        self.insights = Insights(logs.log_dir)

    async def propose(
        self, calendar_events: str = "", existing_summary: str = ""
    ) -> list[str]:
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

        earliest = self.logs.earliest_log_date()
        earliest_habit = self.logs.earliest_habit_date()
        if earliest:
            days_of_data = (date.today() - earliest).days + 1
            user_content = (
                f"Review the last {days} days.\n"
                f"Note: the bot has only been running since {earliest} ({days_of_data} day{'s' if days_of_data != 1 else ''} of data). "
                f"There is no data before that date — absence of logs before {earliest} is not a behavioral pattern, "
                f"it simply means the system did not exist yet. Do not comment on or penalize low coverage for the full {days}-day window.\n"
            )
        else:
            user_content = f"Review the last {days} days.\n"
        if earliest_habit:
            habit_days = (date.today() - earliest_habit).days + 1
            user_content += (
                f"Note: habit tracking started {earliest_habit} ({habit_days} day{'s' if habit_days != 1 else ''} ago). "
                f"Low habit log counts are expected — do not flag them as a pattern.\n"
            )
        user_content += "\n"
        if stats_text:
            user_content += f"{stats_text}\n\n"
        if history:
            user_content += f"{history}\n\n"
        if metrics_text:
            user_content += f"{metrics_text}\n\n"
        tod_text = self.logs.format_time_of_day_for_prompt(days=max(days, 14))
        if tod_text:
            user_content += f"{tod_text}\n\n"
        baseline_text = self.baseline.format_for_prompt()
        if baseline_text:
            user_content += f"{baseline_text}\n\n"
        ledger_text = self.insights.format_for_prompt()
        if ledger_text:
            user_content += f"{ledger_text}\n\n"
        daily_summaries = self._read_daily_digests(days=days)
        if daily_summaries:
            user_content += f"Daily summaries (this week):\n{daily_summaries}"
        else:
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
                        "- Log entries tagged #wrong are explicit user-flagged prompt failures — the bot proposed or did something it shouldn't have. Surface these in the digest and suggest which context file (agenda-rules.md, review-rules.md, etc.) should be updated to prevent recurrence.\n"
                        "- Habits (defined in habits.md) are NOT tracked via the agenda. Do not infer whether habits were completed or missed from agenda data. If a habit appears in the agenda history, ignore its completion status — it proves nothing about whether the habit was actually done.\n"
                        "- Habit completion IS tracked via explicit `habit:` log entries. The stats include a Habit log table showing which habits were logged and on how many days. Use this as the authoritative source for habit adherence. Absence from the habit log on a given day means the habit was not logged — not necessarily that it wasn't done.\n"
                        "- Shabbat (Saturday) is intentionally offline — habits are never tracked on Shabbat. The maximum possible habit logging frequency is 6 days per week, not 7. Never flag Shabbat as a missed day or treat a 6/6 week as anything other than perfect.\n"
                        "- Step counts on Friday and Saturday are structurally low due to Shabbat — do not use raw step averages. The metrics include a pre-computed average excluding Fri/Sat; use that figure when referencing step activity.\n"
                        "- Days with a skip entry (visible in the stats as '⚠️ skip: <reason>') had an external constraint that made certain habits impossible or irrelevant. Use the reason to infer which habits are excused and remove those days from the denominator for affected habits — they are not misses.\n"
                        "- The historical baseline includes weekly average Mood (1-5) and Energy (1-3). Use these for longitudinal context: if mood or energy is trending down (or up) across multiple weeks/months, that is a real, citable pattern — surface it. A single low week is not a trend; a multi-week drift is. Do not diagnose causes you can't see — state the trend and connect it to logged events only when the link is explicit.\n"
                        "- If a 'Mood/energy by time of day' breakdown is provided, use it to surface diurnal patterns the user explicitly wants to understand — e.g. whether mornings, afternoons, or evenings run reliably lower or higher. Only call a time-of-day difference real if the gap is meaningful and the n is not tiny. Where the logs show a recurring event around a low-mood window (e.g. Friday Shabbat-prep stress, a flaky chavrusa, a bad night's sleep), name the likely trigger — but only when the log makes the link explicit, never as invented psychology.\n"
                        "- If an insight ledger is provided, it holds the user's own recurring reflections (hypotheses, ideas, concerns) distilled from past logs, with a 'raised N×' recurrence count. A reflection the user keeps returning to — especially a concern or a hypothesis with a rising count — is exactly the kind of non-obvious, durable pattern the Insight line should surface. Reflect it back as their own observation; never relabel, therapize, or moralize it."
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
        # 7 days: this week only — the daily digest reviews today, not a fortnight
        history = self._completion_history(days=7)
        stats_text = self.logs.format_stats_for_prompt(days=7)

        d = target_date or date.today()
        tomorrow = d + timedelta(days=1)
        earliest = self.logs.earliest_log_date()
        now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
        user_content = f"Date: {d} ({day_type()}). Current time: {now_il.strftime('%H:%M')} Israel time.\n"
        user_content += f"Tomorrow: {tomorrow.strftime('%A')} {tomorrow} ({_day_type_for(tomorrow)}).\n"
        earliest_habit = self.logs.earliest_habit_date()
        if earliest:
            days_of_data = (d - earliest).days + 1
            user_content += (
                f"Note: the bot has only been running since {earliest} ({days_of_data} day{'s' if days_of_data != 1 else ''} of data). "
                f"Absence of logs or stats before {earliest} means the system did not exist — not a behavioral gap.\n"
            )
        if earliest_habit:
            habit_days = (d - earliest_habit).days + 1
            user_content += (
                f"Note: habit tracking started {earliest_habit} ({habit_days} day{'s' if habit_days != 1 else ''} ago). "
                f"Low habit log counts before that date are not a pattern — the system didn't exist.\n"
            )
        day_difficulty = self.logs.read_day_difficulty(d)
        if day_difficulty == "hard":
            user_content += (
                "Day assessment: HARD DAY. Mood and/or energy readings were significantly low. "
                "External disruptions are logged. Adjust the digest accordingly:\n"
                "- Wins section: lead with acknowledgment of what held together despite the difficulty\n"
                "- Improve section: keep it to one line max, or omit entirely if everything that slipped had a clear external cause\n"
                "- Suggestions: recovery only — sleep, tomorrow's one small thing, nothing more\n"
                "The user does not need to be held accountable today. They need to feel seen.\n"
            )
        elif day_difficulty == "good":
            user_content += "Day assessment: GOOD DAY. Energy and mood were positive — hold to a higher standard and push constructively.\n"

        agenda_text = self.logs.read_agenda_as_text(d)
        if agenda_text:
            user_content += (
                f"\nAgenda for {d} (what was planned and its status):\n{agenda_text}\n"
            )
        user_content += f"\nLog for {d}:\n{self.logs.read_day_as_text(d)}\n\n"
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
                        "The current time is provided. If it is before midnight, the day is not yet over — "
                        "do not penalise incomplete agenda items that there is still time to do. "
                        "Calibrate expectations to what is realistically completable given the hour.\n\n"
                        "Weekly stats and history are provided as CONTEXT AND BASELINE ONLY. "
                        "Do not critique, score, or suggest improvements based on weekly trends in a daily digest — that is for the weekly review. "
                        "Focus entirely on today. Use the week to calibrate what's normal, not as a source of feedback.\n\n"
                        "The agenda for the day (if provided) shows exactly what was planned and whether it was done or missed. "
                        "The Improve section must only reference things that were actually on the agenda and missed, or things explicitly logged as problems. "
                        "Do not flag the absence of something that was never on today's agenda — that is not a miss, it simply was not planned for today. "
                        "Each day type has different responsibilities: Haki work belongs on Mon/Wed/Fri, job search on Tue/Thu, "
                        "marketability/income on Sunday. Do not critique the absence of one day type's work on a different day type.\n\n"
                        "Shabbat (Saturday) is intentionally offline — habits are never tracked on Shabbat. "
                        "The maximum possible habit logging frequency is 6 days per week, not 7. "
                        "Never flag Shabbat as a missed logging day.\n\n"
                        "Step counts on Friday and Saturday are structurally low due to Shabbat. "
                        "Use the pre-computed Fri/Sat-excluded average from the metrics section — not a raw average — when referencing step activity.\n\n"
                        "Days with a skip entry (visible in the stats as '⚠️ skip: <reason>') had an external constraint "
                        "that made certain habits impossible or irrelevant. Use the reason to infer which habits are excused "
                        "and remove those days from the denominator for affected habits — they are not misses.\n\n"
                        "## How to write this digest\n\n"
                        "COUNT FIRST, INTERPRET SECOND. Lead with what actually happened — concrete, factual. "
                        "Interpretation is rationed: at most ONE tentative observation in the whole digest, and it "
                        "must be flagged as tentative ('maybe', 'worth watching'), never stated as a verdict. "
                        "Do NOT narrate or diagnose the user's psychology. Do not say things like 'this is shutdown not "
                        "laziness' or 'this is data not failure' — that is presumptuous therapizing. State the fact and stop.\n\n"
                        "NEVER co-opt the user's own words, values, or philosophy as flavor text. If the user has logged "
                        "a principle or metaphor (e.g. about how they want to live or how the system should behave), it is "
                        "theirs — do not quote it back at them or sprinkle it into the digest. That is grating and invasive.\n\n"
                        "NEVER moralize against what the user chose to do. If they spent the day on something — including "
                        "working on this very system — that is their call, not a lapse to correct. Do not tell them what they "
                        "should or shouldn't have worked on.\n\n"
                        "RESPECT THE CLOCK, AND RESPECT ITEM TIMING. The current time is provided. Many anchors have a "
                        "SCHEDULED TIME defined in the user context (habits.md) — e.g. Daf Yomi is 21:00, Yerushalmi 06:15, "
                        "Yoma 10:00–11:00. An item is NOT missed if its scheduled time has not yet passed relative to the "
                        "current time. Daf Yomi is never 'slipped' before 21:00. "
                        "Other items have NO fixed time and can be done any time before the day genuinely ends — the daily "
                        "walk, water, protein. NEVER call a flexible/anytime item missed while the day is still ongoing; "
                        "it can still happen. "
                        "Bottom line: on an unfinished day, do NOT enumerate misses at all, unless an item had a fixed "
                        "scheduled time that has already passed. A thing not-yet-done is not a failure.\n\n"
                        "Never comment on whether the user interacted with the bot itself (reminders answered, checkins sent). "
                        "That is noise, not signal.\n\n"
                        "Keep it SHORT. A tired person is reading this. Terse over thorough. Skip any section that has nothing "
                        "genuine to say — an empty Improve section is fine and often correct, especially on a hard or "
                        "unfinished day.\n\n"
                        "## Format\n\n"
                        '💬 "[short relevant quote]" — [Author, specific source]\n'
                        "(Stoic — Marcus Aurelius/Epictetus/Seneca — or Talmud with tractate+daf or named sage. "
                        "Must connect to the actual day. Must NOT be the user's own logged words.)\n\n"
                        "✅ Wins\n"
                        "- [2-3 specific things that genuinely happened]\n\n"
                        "⬆️ Improve  (OPTIONAL — include only if there is a real, finished miss with no external cause; "
                        "omit the whole section otherwise)\n"
                        "- [at most 1 thing, stated once, no moralizing]\n\n"
                        "💡 Suggestion  (OPTIONAL — at most 1, small and concrete, omit freely)\n"
                        "- [one small option, phrased as an option, NOT a command]\n\n"
                        "HARD STOP after the last section. Do NOT append any closing paragraph, coda, reassurance, "
                        "or summary line (no 'you held the core', no 'that counts', no 'sleep well'). The sections end "
                        "the digest. Trailing commentary is the therapizing voice sneaking back in — it is forbidden.\n\n"
                        "The Suggestion is an OPTION, never an instruction. Do not issue commands about what to do with "
                        "the evening ('rest', 'sleep', 'don't compensate'). State a small possible next step and stop. "
                        "The user decides; you don't direct.\n\n"
                        "Tone: a trusted friend who respects your autonomy and your fatigue. Direct, brief, warm. "
                        "No hype. No shame. No lecturing. No therapizing. No generic advice. No coda."
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

        # Include the user's actual logged data (metrics with trends, recent stats) so
        # feedback on "is my weight plan on track?" can use real numbers, not just the
        # stated system from the context files.
        data_block = ""
        metrics_text = self.logs.format_metrics_for_prompt(days=30)
        if metrics_text:
            data_block += f"{metrics_text}\n\n"
        tod_text = self.logs.format_time_of_day_for_prompt(days=30)
        if tod_text:
            data_block += f"{tod_text}\n\n"
        stats_text = self.logs.format_stats_for_prompt(days=7)
        if stats_text:
            data_block += f"{stats_text}\n\n"

        user_content = text
        if data_block:
            user_content = f"{text}\n\n---\nYour actual logged data (use it — don't claim you can't see it):\n\n{data_block}"

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
                        "If the user's logged data is provided below their question, use it directly — do not say you lack visibility into data that is present. "
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
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()

    async def extract_insights(self, days: int = 7) -> dict:
        """Read the recent logs, distil durable reflections, and persist them.

        The edge of the "AI at the edges" design for the insight ledger: this call reads the
        raw free-form logs the user wrote and decides which sentences are durable reflections
        worth keeping (vs. status/logistics noise), categorising each and matching it against
        the existing ledger so recurrences are linked rather than duplicated. Storage is
        handled deterministically by Insights.merge — this method only proposes.

        Returns the merge summary ({"added", "recurred", "total"}).
        """
        logs_text = self.logs.read_recent(days=days)
        existing = self.insights.open_items()
        existing_block = (
            "\n".join(f"  [{it['id']}] ({it['kind']}) {it['text']}" for it in existing)
            or "  (none yet)"
        )

        client = anthropic.AsyncAnthropic(max_retries=4)
        response = await client.messages.create(
            model=self.model,
            # Generous: a week of logs can yield many reflections, and a forced
            # tool call that runs out of tokens returns truncated (empty) JSON.
            max_tokens=4096,
            tools=[
                {
                    "name": "record_reflections",
                    "description": (
                        "Record durable personal reflections distilled from the user's raw logs."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "new_items": {
                                "type": "array",
                                "description": "Reflections not already in the existing ledger.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "enum": list(INSIGHT_KINDS),
                                        },
                                        "text": {
                                            "type": "string",
                                            "description": "The reflection in the user's own framing, one sentence, faithful to what they wrote.",
                                        },
                                    },
                                    "required": ["kind", "text"],
                                },
                            },
                            "recurrences": {
                                "type": "array",
                                "description": "IDs of existing ledger items the logs touch on again.",
                                "items": {
                                    "type": "object",
                                    "properties": {"id": {"type": "integer"}},
                                    "required": ["id"],
                                },
                            },
                        },
                        "required": ["new_items", "recurrences"],
                    },
                }
            ],
            tool_choice={"type": "tool", "name": "record_reflections"},
            system=[
                {
                    "type": "text",
                    "text": (
                        "You distil a personal-ops user's raw logs into a durable ledger of their own "
                        "recurring reflections. Read the logs and extract only DURABLE reflections — the "
                        "things worth remembering weeks later:\n"
                        "- insight: a realisation about themselves, their patterns, or their work.\n"
                        "- hypothesis: a tentative causal or predictive claim ('Friday anxiety comes from Shabbat-prep stress').\n"
                        "- idea: something to build, try, or change (a feature, a system change, an experiment).\n"
                        "- concern: a recurring struggle or worry worth tracking ('I keep missing Shacharit').\n\n"
                        "STRICT RULES:\n"
                        "- Use the user's OWN framing and words. Do not editorialise, diagnose, or improve their wording. Store what they said.\n"
                        "- Extract only what is genuinely in the logs. Never invent, infer motives, or add reflections they didn't voice.\n"
                        "- SKIP pure status, logistics, and activity logs (ate lunch, had a call, did Anki, drank water). Those are not reflections.\n"
                        "- An existing ledger is provided with ids. If a log touches a reflection ALREADY in it, return its id under recurrences — do NOT create a near-duplicate new item.\n"
                        "- Only add a new_item if it is genuinely new. When in doubt between new and recurrence, prefer recurrence.\n"
                        "- It is correct to return empty lists if the logs contain nothing durable."
                    ),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Existing ledger:\n{existing_block}\n\n"
                        f"Recent logs (last {days} days):\n{logs_text}"
                    ),
                }
            ],
        )

        for block in response.content:
            if block.type == "tool_use":
                d = block.input
                return self.insights.merge(
                    d.get("new_items", []), d.get("recurrences", [])
                )
        return {"added": [], "recurred": [], "total": len(existing)}

    async def parse_event(self, text: str) -> dict | None:
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[
                {
                    "name": "create_calendar_event",
                    "description": "Parse a natural language event description into structured fields",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                            "start_time": {
                                "type": "string",
                                "description": "HH:MM (24h)",
                            },
                            "duration_minutes": {
                                "type": "integer",
                                "description": "Default 60",
                            },
                            "description": {"type": "string"},
                        },
                        "required": ["summary", "date", "start_time"],
                    },
                }
            ],
            tool_choice={"type": "tool", "name": "create_calendar_event"},
            messages=[
                {
                    "role": "user",
                    "content": f"Today is {date.today()} ({day_type()}). Parse this event: {text}",
                }
            ],
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
            tools=[
                {
                    "name": "create_reminder",
                    "description": "Parse a natural language reminder into structured fields",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "The reminder message. If the user mentions an event/appointment time, KEEP that time in the message (e.g. 'Meeting with Rabbi Haber at 19:55') so they see it when reminded.",
                            },
                            "type": {
                                "type": "string",
                                "enum": ["once", "daily", "weekly", "interval"],
                                "description": "'once' for a one-time reminder (default), 'daily' if user says 'every day', 'weekly' if user says 'every [weekday]', 'interval' for repeating every N minutes",
                            },
                            "day_of_week": {
                                "type": "string",
                                "enum": [
                                    "monday",
                                    "tuesday",
                                    "wednesday",
                                    "thursday",
                                    "friday",
                                    "saturday",
                                    "sunday",
                                ],
                                "description": "Required for 'weekly' type — the day to fire on",
                            },
                            "date": {
                                "type": "string",
                                "description": "YYYY-MM-DD — required for 'once'. Resolve relative dates ('tomorrow', 'in a week', 'June 23rd') against today.",
                            },
                            "time": {
                                "type": "string",
                                "description": (
                                    "HH:MM in 24-hour format — the time the REMINDER SHOULD FIRE (not necessarily the event time). "
                                    "Convert 12h to 24h: 7:55 p.m. = 19:55, 8:00 a.m. = 08:00, 12:00 p.m. = 12:00, 12:00 a.m. = 00:00.\n"
                                    "Lead-time handling: if the user wants to be reminded BEFORE an event, compute the fire time. "
                                    "Examples: 'remind me 2 hours before my 7:55pm meeting' -> fire time 17:55. "
                                    "'remind me at 6 about the 7:55 meeting' -> fire time 18:00. "
                                    "'remind me 30 min before my 9am call' -> fire time 08:30. "
                                    "If no lead time or separate reminder time is given, fire time = event time."
                                ),
                            },
                            "interval_minutes": {
                                "type": "integer",
                                "description": "Minutes between reminders — required for 'interval'",
                            },
                            "window_start": {
                                "type": "string",
                                "description": "HH:MM — only set if user specifies a start time. System default: 08:00.",
                            },
                            "window_end": {
                                "type": "string",
                                "description": "HH:MM — only set if user specifies an end time. System default: 22:00.",
                            },
                        },
                        "required": ["text", "type"],
                    },
                }
            ],
            tool_choice={"type": "tool", "name": "create_reminder"},
            messages=[
                {
                    "role": "user",
                    "content": f"Today is {date.today().isoformat()}. Parse this reminder: {text}",
                }
            ],
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        return None

    async def estimate_food(self, text: str, correction: str = "") -> dict | None:
        """Itemise a described meal and estimate per-item + total nutrition from knowledge.

        Estimates macros for ordinary descriptions like "lasagna and a side salad" from
        general knowledge — no per-item values required from the user. The estimate is
        approximate and meant to be confirmed/adjusted by the user before logging.
        `correction` carries the user's portion fixes on a re-estimate. Returns
        {"items": [...], "total": {...}} or None if nothing usable was parsed.
        """
        client = anthropic.AsyncAnthropic()
        user = f"Meal: {text}"
        if correction:
            user += (
                f"\n\nThe user corrected the previous estimate: {correction}\n"
                "Re-estimate the whole meal taking the correction into account."
            )
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[
                {
                    "name": "estimate_meal",
                    "description": (
                        "Break a described meal into its component food items and estimate "
                        "nutrition for each from general knowledge."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "portion": {
                                            "type": "string",
                                            "description": "Estimated portion, e.g. '~300g' or '1 cup'. Use the user's quantity if given, else a typical serving.",
                                        },
                                        "kcal": {"type": "number"},
                                        "protein_g": {"type": "number"},
                                        "fat_g": {"type": "number"},
                                        "carbs_g": {"type": "number"},
                                    },
                                    "required": [
                                        "name",
                                        "portion",
                                        "kcal",
                                        "protein_g",
                                    ],
                                },
                            }
                        },
                        "required": ["items"],
                    },
                }
            ],
            tool_choice={"type": "tool", "name": "estimate_meal"},
            system=(
                "Estimate the nutrition of a described meal. Split it into its component food "
                "items. For each item give a realistic portion (honour any quantity/weight the "
                "user stated; otherwise assume a typical serving) and estimate kcal, protein, "
                "fat, and carbs for that portion from general nutritional knowledge. Estimates "
                "are approximate — that is expected and fine."
            ),
            messages=[{"role": "user", "content": user}],
        )
        for block in response.content:
            if block.type == "tool_use":
                items = block.input.get("items", [])
                if not items:
                    return None
                total = {
                    "kcal": round(sum(i.get("kcal", 0) for i in items)),
                    "protein_g": round(sum(i.get("protein_g", 0) for i in items), 1),
                    "fat_g": round(sum(i.get("fat_g", 0) for i in items), 1),
                    "carbs_g": round(sum(i.get("carbs_g", 0) for i in items), 1),
                }
                return {"items": items, "total": total}
        return None

    async def evaluate_hypothesis(self, text: str) -> dict:
        """Evaluate a hypothesis and return structured tracking actions + narrative.

        Returns a dict with:
          - narrative: str — the response to show the user
          - metrics: list of {"key": str, "description": str}
          - habits: list of str — habit names to watch
          - follow_up_days: int — days until check-in
          - reminders: list of {"text": str, "date": str (YYYY-MM-DD), "time": str (HH:MM)}
        """
        client = anthropic.AsyncAnthropic(max_retries=2)
        today = date.today().isoformat()
        response = await client.messages.create(
            model=self.model,
            max_tokens=800,
            system=[
                {
                    "type": "text",
                    "text": (
                        "You are a thinking partner helping the user stress-test a hypothesis. "
                        "Analyze the hypothesis and call the setup_hypothesis_tracking tool to structure your response.\n\n"
                        "The narrative should:\n"
                        "1. Restate the hypothesis sharply in one sentence\n"
                        "2. Name what would confirm or falsify it\n"
                        "3. Briefly explain what tracking you're setting up and why\n\n"
                        "The tracking should be bot-native — metrics to log, habits to watch, "
                        "a follow-up reminder in 2-3 weeks. Keep it minimal: 1-2 metrics max, "
                        "only habits that are genuinely relevant. "
                        "Metric keys should be short snake_case strings (e.g. shami_cards, retention_score). "
                        "Be direct. No generic advice. No preamble."
                    ),
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"## User context\n\n{self.context.load_all()}",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            tools=[
                {
                    "name": "setup_hypothesis_tracking",
                    "description": "Structure the hypothesis evaluation with narrative and tracking actions",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "narrative": {
                                "type": "string",
                                "description": "The response to show the user — restatement, confirm/falsify conditions, brief tracking rationale",
                            },
                            "metrics": {
                                "type": "array",
                                "description": "Metric keys to track. 1-2 max.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "key": {
                                            "type": "string",
                                            "description": "Short snake_case key, e.g. shami_cards",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "What to log and when",
                                        },
                                    },
                                    "required": ["key", "description"],
                                },
                            },
                            "habits": {
                                "type": "array",
                                "description": "Existing or new habit names to watch as signals",
                                "items": {"type": "string"},
                            },
                            "follow_up_days": {
                                "type": "integer",
                                "description": "Days until a follow-up check-in reminder (typically 14-21)",
                            },
                            "follow_up_note": {
                                "type": "string",
                                "description": "What to check at the follow-up — 1 sentence",
                            },
                        },
                        "required": [
                            "narrative",
                            "metrics",
                            "follow_up_days",
                            "follow_up_note",
                        ],
                    },
                }
            ],
            tool_choice={"type": "tool", "name": "setup_hypothesis_tracking"},
            messages=[
                {"role": "user", "content": f"Today is {today}. Hypothesis: {text}"}
            ],
        )

        for block in response.content:
            if block.type == "tool_use":
                d = block.input
                follow_up_date = (
                    date.today() + timedelta(days=d["follow_up_days"])
                ).isoformat()
                return {
                    "narrative": d["narrative"],
                    "metrics": d.get("metrics", []),
                    "habits": d.get("habits", []),
                    "follow_up_days": d["follow_up_days"],
                    "follow_up_date": follow_up_date,
                    "follow_up_note": d["follow_up_note"],
                }

        return {
            "narrative": "Couldn't evaluate hypothesis.",
            "metrics": [],
            "habits": [],
            "follow_up_days": 14,
            "follow_up_date": "",
            "follow_up_note": "",
        }

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
            f'- "{text}": done {c["done"]}/{c["total"]}, missed {c["missed"]}/{c["total"]}'
            for text, c in sorted(counts.items(), key=lambda x: -x[1]["total"])
            if c["total"] >= 2
        ]
        return ("Completion history:\n" + "\n".join(lines)) if lines else ""

    def _read_daily_digests(self, days: int = 7) -> str:
        # Read saved *-daily.md files from the past `days` days.
        # These are the nightly end-of-day digests — structured narratives that replace
        # raw log dumps in the weekly digest prompt.
        digest_dir = self.context.dir / "digests"
        sections = []
        for i in range(days, -1, -1):
            d = date.today() - timedelta(days=i)
            path = digest_dir / f"{d}-daily.md"
            if not path.exists():
                continue
            text = path.read_text().strip()
            # Strip YAML frontmatter
            if text.startswith("---"):
                parts = text.split("---", 2)
                text = parts[2].strip() if len(parts) >= 3 else text
            if text:
                sections.append(f"### {d}\n{text}")
        return "\n\n".join(sections)

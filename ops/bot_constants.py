import re

STATUS_ICONS = {
    "open": "⌛",
    "done": "✅",
    "missed": "❌",
}

PREFIXES = {
    "insight:": "#insight",
    "hypothesis:": "#hypothesis",
    "checkin": "#checkin",
    "task:": "#task",
    "note:": "#note",
    "did:": "#win",
    "habit:": "#habit",
    "wrong:": "#wrong",
    "backlog:": "#backlog",
    "someday:": "#backlog",
    "food:": "#food",
    "ate:": "#food",
    "ate ": "#food",
    "injection:": "#injection",
    "shot:": "#injection",
    "jab:": "#injection",
    "skip:": "#skip",
    "excuse:": "#skip",
    "excused:": "#skip",
    "values:": "#values",
    "value:": "#values",
}

ENCOURAGEMENTS = [
    "Look at you, a functioning adult!",
    "Your future self just breathed a sigh of relief.",
    "Scientists confirm: doing things is better than not doing things. 🧪",
    "Task defeated 🤺. It never stood a chance.",
    "You absolute legend. Probably.",
    "Gold star 🌟. Imaginary, but still.",
    "This is going straight to your permanent record. The good one. 📓",
    "Somewhere a productivity guru is shedding a single tear of joy. 🥲",
    "Your mom would be proud. Assuming she cares about task management.",
    "That task is dead. You killed it. No regrets. 🪦",
    "Wow. Just... wow. (Keep going.)",
    "The dopamine was real. Ride it. 👊",
]


HELP_INTRO = "🤖 <b>Personal Ops</b> — pick a category:"

# Help is a tap-through menu: each category is (button title, body). Keys are used in
# the callback data (help:<key>). HELP_TEXT below is the flat join, kept as a fallback.
HELP_SECTIONS = {
    "planning": (
        "📅 Planning & Agenda",
        """/plan — generate today's agenda (also daily at 06:00)
/agenda — open items with ✅ Done / ❌ Missed buttons
/status — all items with their status
/queue — queued future agenda items
<code>schedule for Sunday: &lt;item&gt;</code> — add to a future day
<code>done / missed &lt;N or name&gt;</code> — mark an item
<code>add: &lt;text&gt;</code> — add your own item
<code>edit &lt;N&gt; &lt;new text&gt;</code> — edit an item""",
    ),
    "calendar": (
        "🗓 Calendar & Reminders",
        """/events — upcoming events for today
<code>event: &lt;description&gt;</code> — create a Google Calendar event
  e.g. <i>add to calendar: dentist tomorrow at 10am</i>
/reminders — list reminders (tap 🗑 to delete)
<code>remind me &lt;...&gt;</code> — set a reminder
  e.g. <i>remind me at 3pm to start a walk</i>
  e.g. <i>remind me every 60 minutes to drink water</i>""",
    ),
    "habits": (
        "🔥 Habits & Routines",
        """/habits — checklist with streaks 🔥, chain 🟩⬜, ⚠️ flags
/habitcue — set a habit's cue (e.g. <code>/habitcue Daf Yomi: after Maariv, 21:00</code>)
/identity — habits grouped by identities they vote for; a habit can vote for several (<code>/identity Strength: healthy, disciplined</code>; <code>-</code> to remove)
/habitstrategy — a 4-Laws plan for habits you keep missing
/habitnote — note on a habit (no args shows recent notes)
/routines — habit-stack routines (<code>/addroutine</code>, <code>/routinestep</code> to edit)
<code>habit: &lt;name&gt;</code> — log a completed habit
<code>/backdate &lt;when&gt; &lt;entry&gt;</code> — log for a past day
<code>skip: &lt;reason&gt;</code> — excuse habits today""",
    ),
    "review": (
        "📊 Review & Tracking",
        """/daily — end-of-day digest (also nightly at 22:30)
/digest — weekly AI review (also Sundays at 20:00)
/insights — distil recurring insights from your logs
/metrics — tracked metrics with trend (last 14 days)
/weight — Wegovy progress (% lost, rate, chart)
/foodlog — today's food with macro totals
/backlog — someday items, grouped by domain
/logs — today's log entries
<code>injection: &lt;dose&gt;</code> — log a Wegovy injection""",
    ),
    "capture": (
        "✍️ Capture & Logging",
        """<code>food: &lt;what you ate&gt;</code> — nutrition estimate, then log
📷 send a food / nutrition-label <b>photo</b> → macros to confirm
📎 upload an <b>HTML/text file</b> → tasks to /backlog + insights
🎙 <b>voice notes</b> are transcribed automatically
<code>metric: &lt;key&gt; &lt;value&gt;</code> — log a metric
<code>did: &lt;text&gt;</code> — log a win
<code>values: &lt;impression&gt;</code> — a project impression
<code>feedback: &lt;idea/question&gt;</code> — get Claude's take
<code>note: / insight: / task: / hypothesis: / checkin</code>
Anything else is logged as <code>#log</code>""",
    ),
    "context": (
        "⚙️ Context",
        """/context — view and edit your goals, priorities, constraints, projects, principles""",
    ),
}

# Flat join — fallback / any non-interactive use.
HELP_TEXT = "\n\n".join(
    f"<b>{title}</b>\n{body}" for title, body in HELP_SECTIONS.values()
)

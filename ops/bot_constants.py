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


HELP_TEXT = """<b>Planning</b>
/plan — generate today's agenda (also runs daily at 06:00)
/agenda — open items with ✅ Done / ❌ Missed buttons
/status — all items with their current status (done / missed / open)

<b>Calendar</b>
/events — upcoming events for today
<code>event: &lt;description&gt;</code> — create a Google Calendar event
  e.g. <i>new calendar event: PTA meeting April 13th at 4:20pm</i>
  e.g. <i>add to calendar: dentist tomorrow at 10am</i>

<b>Reminders</b>
/reminders — list all reminders (tap 🗑 to delete)
<code>remind me &lt;...&gt;</code> — set a reminder
  e.g. <i>remind me at 3pm to start a walk</i>
  e.g. <i>remind me every 60 minutes to drink water</i>
  e.g. <i>remind me of my meeting on June 15th</i>

<b>Agenda</b>
/queue — view queued future agenda items
<code>schedule for Sunday: &lt;item&gt;</code> — add item to a future day's agenda
<code>done &lt;N or name&gt;</code> — mark item done
<code>missed &lt;N or name&gt;</code> — mark item missed
<code>add: &lt;text&gt;</code> — add your own agenda item
<code>edit &lt;N&gt; &lt;new text&gt;</code> — edit an agenda item

<b>Habits</b>
/habits — today's checklist with streaks 🔥, the chain 🟩⬜, and ⚠️ don't-miss-twice flags
/habitcue — set a habit's cue / implementation intention (e.g. <code>/habitcue Daf Yomi: after Maariv, 21:00</code>)
/identity — habits grouped by the identities they vote for; a habit can vote for several (<code>/identity Strength: healthy, disciplined</code>; prefix <code>-</code> to remove)
/habitstrategy — a 4-Laws plan for habits you keep missing
/habitnote — note on a habit (<code>/habitnote Strength: shoulder felt off</code>); no args shows recent notes
/routines — your habit-stack routines; /routine &lt;name&gt; to view one
<code>/addroutine Morning @06:15: step | step | step</code> — create/edit a routine (linked habits show streaks)
<code>/routinestep Morning: add 3 weigh myself</code> — insert a step (or <code>rm 3</code>) without retyping
<code>habit: &lt;name&gt;</code> — log a completed habit (e.g. <i>habit: walk</i>, <i>habit: daf yomi</i>)
<code>/backdate &lt;when&gt; &lt;entry&gt;</code> — log something for a past day (e.g. <i>/backdate yesterday habit: daf yomi</i>)
<code>skip: &lt;reason&gt;</code> — log an external constraint that excused habits today (e.g. <i>skip: chavrusa cancelled</i>)

<b>Review</b>
/daily — end-of-day digest with quote, wins, and suggestions (also runs nightly at 22:30)
/digest — weekly AI review of the last 7 days (also runs every Sunday at 20:00)
/insights — distil recurring insights/hypotheses/ideas/concerns from your logs (/insights show to view without re-running)
/metrics — tracked metrics with trend (last 14 days)
/weight — Wegovy weight-loss progress (synopsis, % lost, rate, chart)
<code>injection: &lt;dose&gt;</code> — log a Wegovy injection (e.g. <i>injection: 1mg</i>)
/logs — view today's log entries

<b>Context</b>
/context — view and edit your goals, priorities, constraints, projects, principles

<b>Logging</b>
<code>food: &lt;what you ate&gt;</code> — itemised nutrition estimate to confirm/adjust, then log (/food or /foodlog shows today's food with macro totals)
<code>metric: &lt;key&gt; &lt;value&gt;</code> — log a metric (e.g. <i>metric: steps 8000</i>)
<code>did: &lt;text&gt;</code> — log a spontaneous win (tagged <code>#win</code>)
<code>values: &lt;impression&gt;</code> — log a value/impression about the project (/values shows the evolution)
<code>feedback: &lt;idea or question&gt;</code> — get Claude's take (also: "feedback request", "question")
<code>note: / insight: / task: / hypothesis: / checkin</code>
Anything else is logged as <code>#log</code>

Voice notes are transcribed automatically."""

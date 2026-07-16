STATUS_ICONS = {
    "open": "⌛",
    "done": "✅",
    "missed": "❌",
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
/status — day snapshot: open habits, agenda, what's left on the calendar, and a read on how it's going
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
/habitcheck — on-demand end-of-day habit check (also runs nightly)
/addhabit — add a new habit (e.g. <code>/addhabit Stretch [mon,wed,fri]</code>)
/edithabit — edit name, days, or section (e.g. <code>/edithabit Stretch: days=mon,wed,fri</code>)
/managehabits — toggle tracking or delete habits
/habitcue — set a habit's cue (e.g. <code>/habitcue Daf Yomi: after Maariv, 21:00</code>)
/identity — habits grouped by identities they vote for; a habit can vote for several (<code>/identity Strength: healthy, disciplined</code>; <code>-</code> to remove)
/habitstrategy — a 4-Laws plan for habits you keep missing (on demand)
/weeklyhabits — run weekly habit suggestions now (also fires automatically Sunday 09:00)
/habitnote — note on a habit (no args shows recent notes)
/addslip — define a negative habit to track (e.g. <code>/addslip Late wake</code>)
/slip — log a slip; resolves to your tracked list (e.g. <code>/slip slept in: stress</code>)
/slips — summary counts by behavior; <code>/slips Late wake</code> for detail
/manageslips — delete from the negative habit list
/routines — habit-stack routines (<code>/addroutine</code>, <code>/routinestep</code> to edit, <code>/delroutine</code> to remove)
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
/mine — quantitative log-mining: weekday/mood patterns, correlations, habit→mood (<code>/mine advise</code> adds an AI read). Also runs Sundays.
/weight — Wegovy progress (% lost, rate, chart)
/foodlog — today's food with macro totals (net of any retractions)
/macros week|month|quarter|year — rolling macro totals, averages, and foods consumed
/undofood — retract (not delete) a food entry from today
/grocery — shared grocery checklist (<code>/addgrocery</code> to add, <code>/grocerycopy</code> to copy, <code>/cleargrocery</code> to reset)
/backlog — someday items, grouped by domain
/logs — today's log entries
<code>injection: &lt;dose&gt;</code> — log a Wegovy injection""",
    ),
    "capture": (
        "✍️ Capture & Logging",
        """<code>food: &lt;what you ate&gt;</code> — nutrition estimate, then log
🍽 foods you've logged before are recognized automatically (no re-estimating); <code>#default protein shake = 130kcal 24p 0f 3c</code> to seed one yourself
↩️ <code>didn't finish the X</code> / <code>#unlog</code> / <code>scratch that</code> — retract a food entry (never deletes — appends a negation, original stays visible)
📷 send a food / nutrition-label <b>photo</b> → macros to confirm
<code>pick up eggs and milk at the grocery</code> — add grocery items
📎 upload an <b>HTML/text file</b> → tasks to /backlog + insights
🎙 <b>voice notes</b> are transcribed automatically
🎙 start a voice note with <b>"grocery …"</b> to add items to the list
<code>metric: &lt;key&gt; &lt;value&gt;</code> — log a metric
<code>slept 7 hours</code> or <code>/sleep 7</code> — log last night's sleep
<code>did: &lt;text&gt;</code> — log a win
<code>directive: &lt;rule&gt;</code> — a standing instruction to the app (declared, never inferred); /directives lists them
<code>feedback: &lt;idea/question&gt;</code> — get Claude's take
<code>friction: &lt;what went badly&gt;</code> — log drag, blockers, mistakes (<code>wrong:</code> still works)
<code>note: / insight: / task: / hypothesis: / checkin</code>
/hypotheses — open tests + their follow-ups
/fix — reclassify the most recent logged entry
🏷 every logged entry has Edit / Reclassify buttons to correct its tag
➡️ entries classified <code>#task</code> or <code>#backlog</code> get one-tap buttons to route them to the agenda or backlog
Anything else is classified automatically, falling back to <code>#log</code>""",
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


# --- Telegram command menu (the "/" autocomplete list) -----------------------------
#
# Single source of truth for the command menu Telegram shows. `bot.py` pushes this to
# Telegram via `set_my_commands` on startup, so the menu never drifts from the handlers
# again. Aliases (/p, /a, /s, /h, /l, /m, /w, /v, /b, /r, /d) still work but are left out
# of the menu to keep it scannable. Order here is the order Telegram displays.
#
# To regenerate the BotFather paste-list (for @BotFather → /setcommands):
#     python -c "from bot_constants import BOT_COMMANDS; \
#         print('\n'.join(f'{c} - {d}' for c, d in BOT_COMMANDS))"
BOT_COMMANDS = [
    # Daily drivers
    ("plan", "Generate the morning plan for today"),
    ("agenda", "View today's agenda"),
    ("status", "Full status snapshot (agenda + habits + calendar)"),
    ("events", "Today's calendar events"),
    ("queue", "Queued future agenda items"),
    # Habits & routines
    ("habits", "Daily habits checklist"),
    ("habitcheck", "On-demand end-of-day habit check"),
    ("addhabit", "Add a new habit"),
    ("edithabit", "Edit an existing habit"),
    ("managehabits", "Delete or toggle habits"),
    ("habitcue", "Set an implementation intention / habit-stack anchor"),
    ("habitnote", "Attach a note to a habit"),
    ("identity", "Habits grouped by the identities they vote for"),
    ("habitstrategy", "A 4-Laws plan for a habit you keep missing"),
    ("weeklyhabits", "Run weekly habit suggestions now"),
    ("routines", "Habit-stack routines"),
    ("addroutine", "Add a habit-stack routine"),
    ("delroutine", "Delete a routine"),
    ("routinestep", "Add or edit a step in a routine"),
    ("slip", "Log a slip (negative habit)"),
    ("slips", "Slip summary counts by behavior"),
    ("addslip", "Define a negative habit to track"),
    ("manageslips", "Delete from the negative-habit list"),
    # Review & tracking
    ("daily", "End-of-day digest"),
    ("digest", "Weekly AI review"),
    ("insights", "Distil recurring insights from your logs"),
    ("metrics", "Tracked metrics with 14-day trend"),
    ("mine", "Quantitative log-mining report"),
    ("weight", "Weight progress (% lost, rate, chart)"),
    ("foodlog", "Today's food log with macro totals"),
    ("macros", "Macro results for week, month, quarter, or year"),
    ("undofood", "Retract (not delete) a food entry from today"),
    ("backlog", "Someday items, grouped by domain"),
    ("logs", "Today's log entries"),
    ("hypotheses", "Open hypotheses and their follow-ups"),
    ("directives", "Standing directives you've declared"),
    # Reminders
    ("reminders", "List reminders (tap to delete)"),
    # Grocery
    ("grocery", "Shared grocery checklist"),
    ("addgrocery", "Add an item to the grocery list"),
    ("grocerycopy", "Copy the grocery list as text"),
    ("cleargrocery", "Clear the grocery list"),
    # Capture & utilities
    ("backdate", "Log an entry for a past day"),
    ("fix", "Reclassify the most recent logged entry"),
    ("context", "View and edit your goals, priorities, constraints, projects"),
    ("help", "Category menu of everything the bot can do"),
]

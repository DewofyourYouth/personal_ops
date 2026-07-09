Principles
Non-negotiables are non-negotiable
Religious obligations, davening, chavrusa, health — these are schedule anchors, not items to trade away when things get busy. Productivity is measured within these constraints, not against them.
Simplicity and reliability over features
Build things that work and keep working. Personal infrastructure should be boring by design. Don't over-engineer; don't add complexity for its own sake.
Local-first
Data, tools, and workflows should live locally where possible. Avoid depending on external services for core personal infrastructure.
Morning is for deep work; afternoon is for maintenance
Protect the morning window. Shallow tasks, Anki, email, writing, and reading belong in the afternoon. Context-switching in the morning is expensive.
Long-term leverage over short-term optimization
Haki, consulting, writing — these compound. A day job is a bridge, not a destination. Decisions should favor building assets and reducing single points of failure (e.g., one employer, one market).
Streaks and cadences over intensity
Consistent daily output beats occasional sprints. An unbroken Anki streak, weekly writing output, and daily walks matter more than occasional heroics.
Build systems, not to-do lists
The goal is infrastructure that reduces friction and captures signal automatically — not a more elaborate list of things to do. This bot exists for that reason.
Reduce exposure to the Israeli job market
The medium-term goal is location-independent income. Every investment in Haki, consulting capacity, and English-language publishing moves in that direction.
Data Fidelity
The flexible log interface exists because most tracking tools demand precision the user doesn't have — forcing an exact number when only an approximation is known. This produces three failure modes: false precision (entering an approximate as exact), skipped entries, or behavior change to fit the tool (eating the processed food with the label instead of the homemade thing).
This system accepts the user's actual epistemic state instead of requiring them to upgrade it. The following are all valid data:
"Had a big bowl of ice cream, felt like a lot"
"Lower energy than yesterday but not crashed"
"Mostly done, the hard part is done"
When interpreting fuzzy log entries:
Treat approximate descriptions as accurate — do not prompt for precision that doesn't exist
Do not convert vague inputs into false-precise outputs ("~3 scoops" should not become "280 calories")
Pattern recognition over time is more valuable than any single precise data point
When reporting back:
Match output precision to input precision
A fuzzy input deserves a qualitative observation, not a numeric one
This is product philosophy, not interface convenience: the system meets the user's real epistemic state rather than coercing a false one. Demanding precision the user doesn't have is itself a form of agenda laundering — it imposes the tool's need for clean data over the truth of the user's actual knowledge.
No Agenda Laundering
The app must not launder its own agenda through the language of support.
Agenda laundering happens when the system smuggles an unstated value judgment into a suggestion, score, digest, reminder, or interpretation — presenting it as neutral help when it is actually imposing a worldview.
The app may support the user's stated goals. It may notice patterns. It may surface tensions. But it must not quietly decide what kind of person the user should become.
Examples of agendas the app must not launder:
More output is always better.The system must not assume that a day with more completed tasks is automatically better than a day with fewer tasks. Recovery, maintenance, thinking, parenting, religious obligations, and deliberate non-action may all be valid.
Productivity means visible progress. The system must not assume that only shippable, measurable, or logged work counts. Clarifying values, reducing chaos, resting before collapse, or deciding not to pursue something may also be progress.
Health always comes before creativity.
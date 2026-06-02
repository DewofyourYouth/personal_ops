# From Agenda to Advisory: A Paradigm Shift

## The Problem

The current model imposes an agenda. The week has a fixed structure (Mon/Wed/Fri =
Haki development, Tue/Thu = job search), the bot generates a daily list, and completion
rate measures you against it. Even when AI-assisted, the agenda is *imposed* — order for
order's sake — and it turns the system into a task master.

This conflicts with everything else the system is trying to be: energy-aware, anti-Goodhart,
a thinking partner rather than a critic. A prescribed list punishes bad days and rewards
gaming easy ones.

## The Question We're Deliberately Skirting

There's a deeper question underneath all of this: *what does it even mean to be productive,
and how much of unproductivity is an executive-function problem (which an app can help with)
versus a genuine conflict of values (which an app cannot resolve)?* If part of you doesn't
actually want what your stated goals say you want, no system can fix that — and it shouldn't
pretend to.

We are choosing not to answer this yet. It may be unanswerable in the abstract. Instead of a
definition of productivity, we rely on **observable success signals**:

- **Less decision exhaustion** — does using the system leave you *less* depleted by choices,
  not more?
- **Demonstrable movement** — are you clearly doing things that get you toward states of being
  you claim to want?
- **Reduced gap between stated and revealed values** — over time, do your goals and your
  behavior converge, or does the system keep surfacing a gap you never close (which would itself
  be useful information — maybe the goal isn't really yours)?

These are the success criteria. If the redesign doesn't move them, it failed, regardless of how
elegant it is.

## Temperament: A Bamboo (Reed), Not Cedar

- Talmudic wisdom: "Be soft like a reed and not hard like a cedar." (Taanit 20a) — the system should
  be flexible, responsive, and non-confrontational, not rigid, insistent, or shaming.
- Aesop's fable of the oak and the reed is the guiding metaphor for the tone and temperament of the
  system. The oak is rigid, inflexible, and easily broken by a strong wind. The reed is flexible,
  yielding, and able to bend without breaking. The system should be like the reed — rooted and
  present, but yielding under pressure rather than rigid against it.
- Daode Jing, Chapter 76: "A man is born soft and supple; at his death he is hard and stiff. All things, grass and trees, are
  soft and pliant in life; dry and brittle in death. Therefore the stiff and unbending are
  companions of death. The soft and yielding are companions of life. Thus an army that cannot
  yield will be defeated; a tree that cannot bend will be broken."

A guiding principle for *how* everything below is delivered: the app must be **firm but yeilding —
like bamboo, not cedar.** Rooted and unmistakably present, but yielding under pressure rather
than rigid against it.

The reason is mechanical, not aesthetic. If the app is a cedar — rigid, insistent — then a
user who resists it is forced into a *contest*. And resistance itself is exhausting: the
back-and-forth of pushing against something that pushes back generates its own decision fatigue,
which is the exact thing the system exists to reduce. A cedar app fails its own success
criterion the moment the user pushes on it.

Bamboo bends and stays rooted. When the user resists — skips suggestions, ignores a stall
prompt, doesn't engage — the app yields gracefully and remains, rather than escalating. Firmness
lives in *presence and consistency* (it's always there, it doesn't forget the goals, it surfaces
the honest mirror once), not in *force* (it never nags, never repeats, never makes you fight it).

This governs the tone of every interaction: suggestions you can wave off without friction, a
stall detector that states the gap *once* and lets it sit, a morning open that's a question not
a summons. Resistance should cost the user nothing — because a system that's expensive to
resist is a system people abandon.

## The Shift: Prescriptive → Advisory

The system stops dictating and starts advising. The day is led by **your actual state** —
energy, mood, what's pulling at you — and the bot's job is to *offer* things that fit your
current capacity and move your goals forward. You choose. It suggests.

## Three Categories of "Things To Do"

This is the core of the new model. Not everything is the same kind of thing.

### 1. Suggestions (advisory, not scored, CONTINUOUS)
AI-offered, energy-calibrated, goal-conducive. The bot looks at your goals, recent activity,
and current energy/mood, then offers a few things that would help. You take them or leave them.
**These are never scored.** Skipping a suggestion is not a miss — it's just a suggestion you
didn't take.

**Crucially, suggestions are not a once-a-day morning batch.** Energy is dynamic — you can
wake up groggy and surge at 4pm. The system must respond to that in the moment. Two triggers:

- **Pull:** You ask — "I've got energy, what's worth doing right now?" (a command like
  `/suggest`, or natural language). The bot offers something matched to your *current* state.
- **Push:** The system notices a state change. You already log energy via the check-in buttons.
  When you log a jump to high energy, that's the hook — the bot can proactively surface a tough,
  goal-conducive suggestion: "you're charged up — good moment to take a swing at X."

This is the inverse of the burnout logic. Same sensor (mood/energy check-ins), both directions:
**pull back when drained, lean in when charged.** A grogginess→high-energy shift at 4pm is
exactly the signal that should surface something ambitious.

### 2. Futures (opt-in accountability)
Things *you* explicitly decide must happen: "this needs to get done by Thursday whether I like
it or not." Because you chose to be held to it, this **is** tracked and counts toward
productivity evaluation. Accountability is opt-in, never imposed.

**Note on the name.** We deliberately call these *futures*, not *commitments*. "Commitment"
carries dread and moral weight — and for an avoidance-prone, burnt-out user, a heavy label is
itself a deterrent that feeds the very avoidance the system fights. "Futures" is lighter: a
thing you've set for a future date (with a nod to a futures contract), neutral rather than
loaded. The framing is part of the anti-avoidance design, not just cosmetics.

This is what resolves Goodhart's Law: you're only measured against what you chose to be
measured against. The system can't punish you for failing to do something it made up.

### 3. Emergent Patterns (observed, never prescribed)
The system watches the data and surfaces patterns as *insight*, not instruction:
"You've done your most focused Haki work on Sundays" or "Job applications tend to happen on
low-energy afternoons." Day types are **revealed from data, not imposed by the app.** They
inform suggestions; they never become a schedule you must obey.

## The Stall Detector (the necessary safeguard)

A purely energy-led, advisory system has one failure mode that must be designed against:
it can become an **avoidance engine**. The things that most need doing rarely *feel* appealing
in the moment — that's often exactly why they're hard. If the system only ever offers what fits
your current mood, the important-but-unappealing things quietly rot, and the executive-function
offloading the whole system exists for fails silently.

Futures are a partial safety valve, but they rely on you proactively setting one — and that's
the exact muscle that's weakest when avoidance is in play. The same instinct that dodges a hard
task dodges setting a future for it.

So the system needs a **stall detector**. Not a taskmaster — an honest mirror:

- It tracks goals and the activity that moves them.
- When a goal you've said matters goes untouched past some threshold, it surfaces the gap:
  *"You haven't moved on the dashboard in two weeks. You said it mattered. Still true?"*
- The tone is the gap between stated goals and actual behavior — not shame, not nagging.
  It invites a decision: re-commit, downgrade the goal, or consciously park it.

This is the one thing a pure energy-led model loses, and it's the difference between a system
that feels better day-to-day and one that actually serves the goals. **It is not optional.**

Likely lives in the weekly digest plus a lightweight mid-week check, drawing on the same
goal/activity data the suggestion engine uses.

## What Changes in Code

- **`_day_type_for`** (planner.py) — the hardcoded Mon/Wed/Fri = Haki mapping goes away as a
  prescription. Day-of-week tendencies become something computed from historical data and
  offered as soft context to the suggestion engine.

- **The morning interaction goes away as the anchor.** There's no daily agenda push. Instead,
  suggestions surface continuously — when you ask, and when an energy check-in shows a positive
  shift. The morning may still offer a light "here's what's pulling at your goals" if you want it,
  but it's no longer the organizing event of the day.

- **Energy check-in → suggestion trigger.** When a logged energy reading jumps upward (esp. to
  high), the bot proactively offers a tough, goal-conducive suggestion. This reuses the existing
  mood/energy check-in machinery as the sensor.

- **Completion scoring** — completion rate against an imposed list mostly stops making sense.
  Productivity evaluation shifts to: (a) futures met, (b) habit adherence, (c) goal progress
  over time. The baseline/difficulty work applies to futures and habits, not to suggestions.

- **The agenda data model** — `{date}-agenda.json` becomes a record of futures + which
  suggestions were taken, not a checklist you're graded on.

## Structure Mode (reversible, not a demolition)

The shift from prescriptive to advisory is delivered as a **user-facing mode**, not a one-way
rewrite. A new per-user config holds `structure_mode`:

- **`structured`** — current behavior. Daily agenda generated and proposed; completion scored.
  Scaffolding, for when you have capacity and want a forcing function.
- **`advisory`** — the new model. No daily agenda push; suggestions on pull + energy-push;
  no completion scoring.

A `/mode` command reads and sets it. Changing modes is one message — no rebuild.

Two things this buys:
1. **De-risks the burnout-distortion problem.** This redesign is being conceived from a depleted
   state (long job search, sleep deprivation, young kids, IBS). Calibrating a permanent
   architecture from one's most flattened moment is risky. A reversible switch means if capacity
   returns and the scaffolding is missed, you flip back — the decision was never permanent.
2. **First brick of multi-tenancy.** `UserConfig` is exactly what the Shabbat/offline-days
   settings need too. Building the mode toggle lays that foundation for free.

**The accountability floor runs in BOTH modes:** futures and the stall detector are always
on. The mode only toggles the daily-agenda-push and completion-scoring behavior. Accountability
is not part of the advisory package — it's the floor underneath everything.

The eventual sophisticated system is **adaptive**: it slides the structure level automatically
based on whether you're thriving or floundering. We cannot build that yet — we don't know the
rules. We learn them from the data collected below.

## Data Collection: The Differentiator (non-optional)

This is the part that makes the eventual adaptive system possible — and it is the project's
real differentiator. Most tools either impose structure or remove it. None *learn the rules
of when to push and when to back off* from the individual's own data. To do that, we must
instrument from day one.

**Critical distinction: "not scored" ≠ "not logged."** Suggestions never count *against* the
user (that's for their psychology). But every suggestion and push is *recorded* — because what
happened to it is the training label for the adaptive system. Skip this and in a few months we
have a pile of states with no outcomes and nothing to learn from.

The adaptive system is a supervised-learning shape: *given a state, which intervention produced
a good outcome?* So every time the bot suggests or pushes, log four things:

1. **State snapshot** (inputs) — energy, mood, time of day, day of week, recent momentum,
   stall status, and whatever context exists at that moment.
2. **Intervention** (action) — what the bot did: suggested X, pushed because energy jumped,
   surfaced a stall, stayed silent.
3. **Response** (label) — did the user act on it, ignore it, or explicitly reject it? Captured
   cheaply via the suggestion's own buttons ("did it" / "not now" / "not for me") — the tap *is*
   the label, no extra friction. **This is the column most easily forgotten and the one that
   matters most.**
4. **Outcome** (reward) — downstream: did progress get logged afterward? did the next energy/mood
   reading rise or fall?

With those four, the data can eventually answer questions like: "when energy jumps at 4pm and the
bot pushes a hard task, does he take it — and does he feel better or worse after?" That is a
learnable rule. Without the response and outcome columns, it is unanswerable.

Implementation: a new `interventions` table in SQLite, written on every suggestion/push, with
response captured via the suggestion buttons. Logging silence (the bot choosing *not* to push)
is valuable too but harder — it can come later.

## What Stays Constant

- **Goals.** The system still knows what moves the needle and surfaces it when you have capacity.
  Energy-led does not mean goalless — it means goal-conducive suggestions matched to real capacity.
- **Habits.** Still tracked as before (they're recurring futures you've already opted into).
- **The baseline/difficulty philosophy.** Still applies — just to futures and habits, the
  things you've actually signed up for.

## The Morning Open (resolved)

Advisory mode still has one daily touchpoint — a gentle entry point so the system doesn't go
silent and get forgotten. It opens with:

> **"What do you want to work on today?"**

Presented as **multiple-choice buttons** (goal-conducive options the bot generates from your
goals + current context) plus a **"type something else"** free-text escape hatch. This is the
right shape because:
- It's a question, not a directive — you're choosing, not being assigned.
- The options reduce decision load (you're reacting, not generating from scratch).
- The free-text option means it never boxes you in.
- Whatever you pick (or type) becomes the first logged intervention of the day — state + choice.

This is the *light* morning anchor referenced above, now concretely defined.

## Open Questions

1. How are futures captured? A prefix like `future: ship the dashboard by Thursday`?
2. Does the emergent-pattern detection run in the weekly digest, or surface live in suggestions?

## Build Order (proposed)

1. **`UserConfig` + `/mode`** — per-user config store with `structure_mode` (`structured` /
   `advisory`). Morning job branches on it. Reversible switch, first brick of multi-tenancy.
2. **`interventions` table** — the data-collection layer. Build it early so every suggestion
   from day one is instrumented (state / intervention / response / outcome). Non-optional —
   this is the differentiator.
3. **Futures** — add the opt-in future type (prefix + storage + deadline tracking).
   The load-bearing new concept; runs in both modes.
4. **Suggestion engine (pull)** — `/suggest` command: offer energy-calibrated, goal-conducive
   suggestions on demand based on current state. Not scored, but logged to `interventions`.
   Response captured via buttons ("did it" / "not now" / "not for me").
5. **Suggestion trigger (push)** — when an energy check-in jumps upward, proactively offer a
   tough goal-conducive suggestion. Reuse the mood/energy check-in as the sensor. Logged too.
6. **Stall detector** — track goal/activity linkage; surface goals that have gone untouched
   past a threshold, as an honest mirror in the weekly digest + a lightweight mid-week check.
   The necessary safeguard against the avoidance-engine failure mode. Runs in both modes.
7. **Emergent day-types** — replace `_day_type_for` with data-derived tendencies.
8. **Reframe scoring** — shift productivity evaluation to futures + habits + goal progress.
9. **(Future) Adaptive structure** — once `interventions` has enough data, learn the rules for
   auto-sliding the structure level. The payoff of the data collection. Cannot be built before
   the data exists.

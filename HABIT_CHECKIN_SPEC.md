# End-of-Day Habit Check-in — Spec / Plan

Status: **proposed** (not built). Authored 2026-06-04.
Anchored to [ops/context/principles.md](ops/context/principles.md); tone governed by the
bamboo temperament in [AGENDA_PARADIGM_SPEC.md](AGENDA_PARADIGM_SPEC.md).

## The problem

Habit tracking currently relies entirely on **user vigilance**: a habit only registers if you
remember to log it (`habit: walk`, the `/habits` buttons, or `hb_done:`). Two failures follow:

1. **Silent under-counting.** Forget to log a habit you actually did → it reads as a miss,
   breaks a streak, and pollutes adherence data. (Same class of "relying on the user to
   remember" bug we just hit with metrics.)
2. **Absence is ambiguous.** A missing log can mean *didn't do it* **or** *did it, forgot to
   log*. `compute_streak` treats absence as a miss — conflating the two. We have **no explicit
   negatives**, so the data can't tell forgetting from not-doing.

The fix: at end of day, the system **asks** about habits that are due today and not yet logged,
and lets you answer did / didn't. This flips habits from passive (you must remember) to active
(it prompts) — and yields explicit "no" data we currently never capture.

## Goal

A once-daily, gentle end-of-day prompt listing **only** the habits that are:
- **due today** (`days` matches today's weekday), and
- **not already logged/done**, and
- **not excused** by a `skip:` today.

Each gets buttons to resolve it: **✅ Did it · ❌ Didn't · ⤬ N/A (excused)**. One message,
updates in place. If nothing qualifies (all logged, or none due), it sends nothing.

## The real win: explicit miss vs. unknown

Today the data model has one habit signal — a positive `habit` log. This adds a second:
an explicit **miss**. After the check-in, each due habit is in one of three states:

| State | How it arises | Meaning |
|---|---|---|
| **done** | logged (any path) or "✅ Did it" tapped | really happened |
| **missed** | "❌ Didn't" tapped | really didn't happen (new — we never had this) |
| **unknown** | check-in ignored / not answered | genuinely no data |

This directly serves the **Data Fidelity** principle: we stop coercing absence into a false
"miss," and we accept the user's actual epistemic state (answered vs. not).

**Streak-compatibility note (decide in build):** `compute_streak` currently does
`done = any(matching log)`, i.e. absence breaks the streak. Introducing explicit misses raises a
real question — should an *unknown* (unanswered) day break a streak the way a *missed* day does?
Recommended: a confirmed **miss** breaks it; an **unknown** is neutral (skipped like Shabbat)
rather than punished, so forgetting to answer isn't treated as failure. This is itself an
anti-Goodhart choice and must be explicit, not incidental.

## Timing & quiet rules

- **Fires before the daily digest**, so the 22:30 digest reflects confirmed adherence rather
  than stale guesses. Default a configurable `EOD_HABIT_HOUR` (e.g. 21:00); digest stays 22:30.
- **Skips Shabbat / quiet hours** — reuse `_shabbat_quiet_now()`.
- **Fires once.** Per the bamboo principle ("states the gap once and lets it sit"), there is no
  re-nag. If you ignore it, those habits stay *unknown*; the system does not chase you.

## Tone (this is a memory aid, not pressure)

`principles.md` explicitly forbids "Accountability means pressure" and "A good user complies."
So the framing is a neutral end-of-day check, not a scold:

> "Quick end-of-day check — did these happen today?"

Not "You forgot to log X." Missing is fine. "Didn't" is fine and carries no shame — habits are
recurring **futures the user opted into** (agenda spec), so *asking* is legitimate opt-in
accountability, but the *answer* is just data. Ignoring it is valid, not disobedience.

## Mechanics (reuse what exists)

- Build the prompt like `_habits_message()` but filtered to **due-today ∧ not-done ∧ not-excused**.
- Buttons per habit: `hb_done:<name>` (already exists → writes a `habit` log), a new
  `hb_miss:<name>` (writes the explicit miss), and optionally `hb_na:<name>` (excused).
- The callback edits the message in place (like `handle_habit_callback`), checking off each
  habit as it's resolved, until the list is cleared.
- Storage for misses: either a new tag (`habit_missed`) in the same log/JSONL+DB path, or a
  `status` column on a habit-events table. Lightweight tag keeps it consistent with current
  logging; the streak/adherence readers learn to honor it.

## Integration

- **Daily digest** reads confirmed done/missed instead of inferring from absence → honest
  adherence numbers.
- **Eval loop / interventions** ([EVAL_LOOP_SPEC.md](EVAL_LOOP_SPEC.md)): the check-in *is* a
  push intervention, and the did/didn't tap *is* the response label — log it as such.
- **Stall detector** (agenda spec): explicit misses are a far cleaner signal than absence for
  "a habit you said mattered has actually lapsed."

## Edge cases

- All due habits already logged → send nothing (no noise).
- No habits due today (e.g. all off-day) → nothing.
- Shabbat / quiet window → skip.
- A habit logged *after* the prompt is sent → its button is a no-op / dedupes (don't double-log).
- User answers some, ignores rest → answered ones recorded; rest stay *unknown* (no follow-up).

## Build order

1. **Filter helper** — "due-today ∧ not-done ∧ not-excused" habit list (extract from
   `_habits_message` logic).
2. **`hb_miss:` callback + miss storage** — record explicit negatives; teach the habit readers
   the new state.
3. **Scheduled EOD job** — `EOD_HABIT_HOUR` (config), Shabbat/quiet-guarded, fire-once, sends the
   filtered prompt only when non-empty.
4. **Streak/adherence update** — confirmed miss breaks streak; unknown is neutral (recommended).
5. **Digest read-through** — digest consumes confirmed states.
6. **(Later) wire to interventions/eval** as a push + response once that table exists.

## Open questions

1. **Does unknown break a streak?** (Recommended: no — neutral, like Shabbat.)
2. **Separate prompt at `EOD_HABIT_HOUR`, or fold the buttons into the 22:30 digest message?**
   (Separate-and-earlier keeps the digest accurate; folding is one fewer message.)
3. **Is `skip:` per-day global, or per-habit?** Affects how "excused" filters the list.
4. **`hb_na` (excused) button — needed at check-in time, or does `skip:` already cover it?**

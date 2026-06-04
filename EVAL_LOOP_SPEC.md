# Eval Loop — Spec / Plan

Status: **proposed** (not built). Authored 2026-06-04.
Sits on top of [AGENDA_PARADIGM_SPEC.md](AGENDA_PARADIGM_SPEC.md) and is graded against
[ops/context/principles.md](ops/context/principles.md).

## The problem (named precisely)

We want a feedback loop on the advice the system gives — every digest, every `feedback:`
reply, every advisory intervention — so it gets *better over time*. The danger is well known:
this is **reward hacking / sycophancy**. If the reward signal is "how much did I like it,"
the optimum is a flatterer — a system that tells you what you want to hear (Soma, not an
advisor). RLHF fails this way; we must design around it from the start.

But the failure is **two-sided**, and `principles.md` already names both ends:

- **Too soft (sycophancy):** softens the honest mirror to keep you comfortable.
- **Too hard (agenda laundering):** smuggles "more output is better / discipline is superior /
  hard things are the truest goals" in as neutral help — the cedar, not the bamboo.

A good advisor lives in the narrow band between them: tells you what you *need* to hear, in a
way you can *take*. The eval's job is to measure position in that band — not approval.

## Core design move: score *delivery* and *substance* separately

Collapsing advice into one "good?" score is what creates the trap. Two orthogonal axes,
two different signal sources:

| Axis | What it measures | May optimize toward comfort? | Anchored to |
|---|---|---|---|
| **A — Delivery (bamboo)** | warm, well-timed, non-coercive, didn't nag, respected resistance | **Yes** — being a jerk isn't the goal | tone rules in agenda spec |
| **B — Substance (mirror)** | named the real thing, true, useful, *did not launder an agenda*, labeled inferences as inferences | **No, never** | `principles.md` + outcomes |

Optimizing A toward "what you want to hear" is fine and desirable. B must be anchored to
truth, your own chosen values, and real outcomes — never to liking. That split is the whole
answer to "in a way that's also what they want to hear": get nicer, don't get softer.

## Signals (weighted so the in-the-moment reaction can't dominate)

Each evaluated item gets a small vector, not a star rating. Listed lightest→heaviest weight:

1. **Directional reaction (immediate, low weight).** Buttons capture *direction of error*, not
   approval: `nailed it · fair · too soft · too harsh · off-base`. The `too soft` / `too harsh`
   options are the anti-sycophancy valve — they let *you* push it to be harder, which a
   like/dislike scale can never do. Feeds A heavily, B lightly.
2. **Behavioral trace (objective, high weight).** Did you act on it? Reuses the interventions
   `response`/`outcome` columns. Action is a far better proxy for "useful" than a smile.
3. **Aged-well (delayed, highest weight on B).** N days later, for a sample: *"that thing it told
   you on the 3rd — right call?"* This is literally the gap between *want* and *need*: good
   advice is often what you resisted Tuesday and were grateful for Friday.
4. **Calibration (objective).** The advisor **pre-registers a prediction** ("you'll stall on the
   dashboard again this week"); reality confirms/refutes later. Rewards being *right*, not
   agreed-with. Pre-registration means it can't be retrofitted.
5. **North-star outcomes (slow, objective).** The agenda spec's success signals: less decision
   exhaustion, demonstrable movement, shrinking stated-vs-revealed gap. The advisor is ultimately
   graded on whether these move — not on any single rating.

**Aggregation rule.** Delayed + behavioral + objective signals dominate. And one explicit guard:
track the **correlation between immediate liking (A) and aged-well/behavioral value (B)**. If the
system is becoming *more liked* without becoming *more useful*, that divergence **is** sycophancy
drift — a monitorable alarm, not a reward.

## The constitution anchor (answers "not steering to my whims")

Substance (axis B) is graded against `principles.md`, not against today's mood — the difference
between a constitution and a mood ring. Concretely, an **LLM-as-judge** grades each item's
substance against the explicit `principles.md` rubric (esp. *No Agenda Laundering* and *"hold up
a mirror, may not decide what the reflection means"*).

The obvious risk — an LLM judge can be sycophantic too — is handled two ways:
- The judge scores **adherence to named principles**, not "is this good" (a rubric, not a vibe).
- Your **delayed + behavioral signals are the ground truth** that *validates the judge over time*.
  If the judge says "great" but it never aged well, the judge is miscalibrated and gets corrected.

## "Weighed properly" = low learning rate

You're n=1 and your ratings are noisy. The loop steers **slowly**:
- No change to advisor prompt/tone from any single rating. Changes require a **trend** across
  many items, reviewed in a periodic **eval review** (e.g. inside the weekly digest).
- High inertia by design — one grumpy evening must not move the system.

## What the loop actually changes (phased — start as a mirror, become a harness)

1. **Scoreboard only.** Collect signals; surface them to *you* (a `/evals` view + a section in
   the weekly digest). Changes nothing automatically. Pure instrumentation + reflection.
2. **Held-out eval set (the "harness").** Curate past `(state → advice/digest → graded outcome)`
   examples into a fixed set. When you change the advisor prompt, **re-run it against the set** and
   have the principles-anchored judge score whether the new version is *more aged-well-aligned and
   less laundering* — an A/B for prompt versions, the rigorous "eval harness" sense.
3. **Slow tuning.** Only once 1–2 have signal: adjust advisor prompt/tone, gated on aged-well +
   behavioral trends, validated against the held-out set. Never real-time reactivity.

## Data model

Reuse and extend the agenda spec's `interventions` table; add two small tables:

- **`eval_items`** — one row per gradeable output: `id, ts, kind (daily_digest | weekly_digest |
  feedback | suggestion | push | stall), ref (link to the source row/log), content_hash`.
- **`eval_judgments`** — `eval_item_id, source (user_immediate | user_delayed | judge | behavioral
  | outcome), axis (delivery | substance), score, direction (nailed/fair/too_soft/too_harsh/
  off_base), note, ts`. Multiple judgments per item over time (immediate, then delayed, then
  outcome) is the point.
- **`predictions`** — `id, ts, eval_item_id, claim, horizon_days, resolved_at, outcome
  (true | false | unclear)`. The calibration channel; pre-registered, resolved later.

## Integration points (existing seams)

- `planner_.feedback()` and the digest builders (`planner_.digest`, `planner_.daily_digest`) —
  each output creates an `eval_items` row and is sent with the directional-reaction buttons.
- The check-in / suggestion buttons already planned in the agenda spec carry the behavioral
  signal — extend, don't duplicate.
- A new scheduled job surfaces a small **delayed re-rating** batch (a few items, M days old) and
  resolves due `predictions` — folded into the weekly digest's eval-review section.
- `/evals` command — read-only scoreboard (axis A vs B trend, the sycophancy-divergence alarm,
  calibration hit rate).

## Build order

1. **`eval_items` + directional buttons** on digests and `feedback:` replies. Capture immediate
   reaction. (Phase-1 scoreboard begins here.)
2. **`eval_judgments`** store + a `/evals` read-only view + a weekly-digest eval section.
3. **Behavioral linkage** — join to interventions response/outcome so "did you act" flows in.
4. **Delayed re-rating job** — sample old items, ask "right call?", record `user_delayed`.
5. **`predictions`** — let the advisor pre-register a claim; a job resolves them; calibration on
   the scoreboard.
6. **LLM-as-judge** graded against `principles.md`; validate it against the human/behavioral
   ground truth already collected.
7. **Held-out eval set + harness** — curate examples; A/B prompt versions against it.
8. **(Slow) tuning** — adjust advisor behavior on trends, gated on aged-well + behavioral, never
   single ratings.

## Open questions

1. **Sampling rate for delayed re-rating** — every item is too much friction; what fraction, and
   at what horizon (3d? 7d?) best separates want from need without becoming a chore?
2. **Who writes predictions** — only the stall detector, or every advisory output gets an optional
   pre-registered claim?
3. **Does the judge see your reaction** when grading substance? (Probably *no* — keep it blind to
   avoid importing your in-the-moment bias into the "objective" channel.)
4. **Failure-to-engage** — if you stop rating, the loop goes blind. Is silence itself a signal
   (low weight), or just missing data?

## Why this is the differentiator (same thesis as the agenda spec)

Most tools optimize engagement, which *is* the sycophancy gradient. The thing almost nobody
builds is an advisor whose own quality metric is deliberately **decorrelated from approval** and
anchored to a stated constitution + real outcomes. That's the hard, valuable part — and it's the
same bet as the agenda paradigm: learn, from one person's data, when to push and when to yield.

# Baseline Tracking Spec

## Problem

The weekly digest currently passes 7 days of raw log entries to the LLM. This works now but
doesn't scale — over time it means dumping every "habit: Yerushalmi" ever logged into the
prompt, and the LLM has no longitudinal context to distinguish a good week from a bad one.

The deeper goal: the digest should be able to say "you're functioning significantly better
than you were three months ago, even if it doesn't feel that way — recalibrate your
expectations." That requires a persistent, compressed history.

## Design Principles

### 1. Raw numbers, no cached interpretations

Monthly and quarterly summaries store only numeric aggregates — averages, ranges, counts.
No AI-generated notes are baked in at compression time. Every weekly digest re-interprets
the full history from scratch. This preserves the ability to spot patterns that only become
visible with more data — including long cycles (seasonal, annual, or longer).

### 2. Goodhart's Law resistance via calibration windows

When a metric becomes a target, it ceases to be a good measure. Users (including this one)
will subconsciously avoid committing hard things to the system to protect their completion
percentage. The fix: every new habit and recurring agenda item gets a **30-day calibration
window** before it enters the baseline.

During calibration:
- Data is collected normally
- The item does not contribute to completion % or baseline comparisons
- The digest labels it as "calibrating" and shows raw counts only
- No judgment, no pattern inference

After 30 days:
- The observed completion rate for that item **becomes its personal baseline**
- A habit done 60% of the time has a 60% baseline — not an implicit 100%
- Improvements and regressions are measured against *that item's own history*, not a
  universal standard

This means a genuinely hard commitment that gets done 5/6 days is correctly read as
excellent, even if a lighter item gets done 6/6.

### 3. Progressive compression

History is kept at three resolutions:

| Tier | Covers | Resolution |
|------|--------|------------|
| Weekly | Last 8 weeks | Full stats per week |
| Monthly | 2–12 months ago | Averages + range per month |
| Quarterly | 12+ months ago | Averages + range per quarter |

Compression runs when the weekly digest is generated (Sunday night). Anything older than
8 weeks rolls into its month; anything older than 12 months rolls into its quarter. The
total number of data points passed to the LLM stays bounded regardless of how long the
system runs.

## Data Structure

Stored in `ops/log/baseline.json`.

```json
{
  "weekly": [
    {
      "week_start": "2026-05-25",
      "completion_pct": 72,
      "anchor_pct": 65,
      "wins": 8,
      "habits": {
        "shacharit": {"logged": 5, "trackable": 6},
        "daf yomi":  {"logged": 6, "trackable": 6},
        "walk":      {"logged": 4, "trackable": 6}
      }
    }
  ],
  "monthly": [
    {
      "month": "2026-04",
      "completion_avg": 68,
      "completion_range": [52, 81],
      "anchor_avg": 61,
      "anchor_range": [44, 74],
      "wins_total": 29,
      "habits": {
        "shacharit": {"logged_avg": 4.8, "trackable_avg": 6.0},
        "daf yomi":  {"logged_avg": 5.5, "trackable_avg": 6.0}
      }
    }
  ],
  "quarterly": [
    {
      "quarter": "2025-Q4",
      "completion_avg": 61,
      "completion_range": [38, 79],
      "anchor_avg": 55,
      "anchor_range": [31, 72],
      "wins_total": 94,
      "habits": {
        "shacharit": {"logged_avg": 4.2, "trackable_avg": 6.0}
      }
    }
  ]
}
```

### Calibration tracking

Stored alongside baseline data, in `ops/log/item_calibration.json`.

```json
{
  "habits": {
    "shacharit": {"first_seen": "2026-05-27", "calibrated": true},
    "rambam":    {"first_seen": "2026-06-10", "calibrated": false}
  },
  "agenda_items": {
    "apply to 3 jobs": {"first_seen": "2026-05-28", "calibrated": true}
  }
}
```

`calibrated` flips to `true` once `first_seen` is more than 30 days ago. Items where
`calibrated: false` are excluded from completion % calculations and flagged separately
in the digest.

## What the Weekly Digest Gets

Instead of 7 days of raw log entries, the weekly digest prompt receives:

1. **This week's daily digest summaries** — the saved `*-daily.md` files, which are already
   structured narratives of each day. These replace raw logs as the week-level narrative layer.
2. **The full baseline history** — all weekly entries (last 8 weeks), monthly summaries,
   quarterly summaries. Rendered as a compact table, not raw JSON.
3. **Calibration status** — a short list of any items still in their calibration window,
   so the LLM knows to treat their data as provisional.

## What the Daily Digest Gets

The daily digest is already in reasonable shape. Changes:

- `_completion_history` window trimmed from 14 → 7 days (this week only, not a fortnight)
- Weekly baseline summary appended as context (not critique fodder — see existing prompt
  instructions)

## Implementation Phases

### Phase 1 — Compression (now)
- `Baseline` class in `ops/baseline_tracker.py`
- `compute_and_save_weekly()` — runs after Sunday digest, compresses old entries
- Weekly digest reads saved daily digests + baseline history instead of raw logs
- Daily digest: trim `_completion_history` to 7 days

### Phase 2 — Calibration (after seeing real baseline data)
- `item_calibration.json` tracking
- `first_seen` detection from log files for existing habits/items
- Exclusion of calibrating items from completion %
- Digest prompt updated to label calibrating items separately

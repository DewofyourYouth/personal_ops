"""Tests for ops/mine_logs.py.

Two things here are easy to get subtly wrong and worth locking in:
- the affect-proxy join (nearest-timestamp matching, gap cutoff) — letting a
  far-apart tap masquerade as ground truth for a voice note would be a silent
  correctness bug.
- the advice-synthesis gate (build_findings/_validate_citations) — this is a
  regression suite for a real incident where the LLM synthesis invented a
  "habits correlated with weight" claim and treated a marginal n=12/13 habit
  delta and a near-zero lagged r as real findings. The fix is structural: the
  LLM must never see a row that hasn't cleared n>=MIN_N_ADVICE and a 95% CI
  excluding zero, and every claim it makes must cite a finding id that
  resolves against what it was actually given.

Everything else in the module (report()'s printed sections) is descriptive
reporting, easy to eyeball from the printed output — not tested here.
"""

import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

from logs import Logs
from mine_logs import (
    AFFECT_MAX_GAP_MIN,
    MIN_N_ADVICE,
    Finding,
    _corr_ci95,
    _mean_diff_ci95,
    _validate_citations,
    advise,
    build_findings,
    load_affect_pairs,
)

TZ = ZoneInfo("Asia/Jerusalem")


def _dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 10, hour, minute, tzinfo=TZ)


def _mood_metric(value: int) -> dict:
    return {"key": "self_mood_rating", "value": value, "unit": ""}


def test_matches_voice_note_to_nearest_mood_tap_within_gap(tmp_path):
    logs = Logs(str(tmp_path))
    logs.write(
        "checkin",
        "voice note",
        extra={"affect_features": {"pitch_var": 12.0, "pause_count": 2}},
        when=_dt(10, 0),
    )
    # nearest tap (5 min later) should win over a further one
    logs.write("metric", "self_mood_rating 4", extra=_mood_metric(4), when=_dt(10, 5))
    logs.write("metric", "self_mood_rating 1", extra=_mood_metric(1), when=_dt(11, 0))

    pairs = load_affect_pairs(sqlite3.connect(logs.db.path))

    assert len(pairs) == 1
    assert pairs[0]["mood"] == 4
    assert pairs[0]["pitch_var"] == 12.0


def test_drops_pairs_wider_than_max_gap(tmp_path):
    logs = Logs(str(tmp_path))
    logs.write(
        "checkin",
        "voice note",
        extra={"affect_features": {"pitch_var": 5.0}},
        when=_dt(9, 0),
    )
    # only tap available is well outside the gap window -> no pair
    far_minute = int(AFFECT_MAX_GAP_MIN) + 15
    logs.write(
        "metric",
        "self_mood_rating 3",
        extra=_mood_metric(3),
        when=_dt(9, far_minute),
    )

    pairs = load_affect_pairs(sqlite3.connect(logs.db.path))

    assert pairs == []


def test_entries_without_affect_features_are_ignored(tmp_path):
    logs = Logs(str(tmp_path))
    logs.write("note", "just a text note", when=_dt(10, 0))
    logs.write("metric", "self_mood_rating 3", extra=_mood_metric(3), when=_dt(10, 1))

    pairs = load_affect_pairs(sqlite3.connect(logs.db.path))

    assert pairs == []


# ── CI helpers ────────────────────────────────────────────────────────────


def test_corr_ci95_excludes_zero_for_a_real_effect():
    lo, hi = _corr_ci95(0.5, 20)
    assert lo == pytest.approx(0.074, abs=0.005)
    assert hi == pytest.approx(0.772, abs=0.005)
    assert lo > 0


def test_corr_ci95_crosses_zero_for_a_weak_effect():
    lo, hi = _corr_ci95(0.05, 20)
    assert lo < 0 < hi


def test_corr_ci95_undefined_for_tiny_n_or_perfect_r():
    assert _corr_ci95(0.5, 3) is None
    assert _corr_ci95(1.0, 20) is None


def test_mean_diff_ci95_degenerate_but_excludes_zero_when_groups_truly_differ():
    """Zero variance in both groups isn't 'uncomputable' if the means still
    differ — the true CI is a point, and that point had better not silently
    read as 'no data' the way se==0 used to."""
    ci = _mean_diff_ci95([5.0] * 20, [1.0] * 20)
    assert ci == (4.0, 4.0)


def test_mean_diff_ci95_degenerate_and_includes_zero_when_groups_are_identical():
    ci = _mean_diff_ci95([3.0] * 20, [3.0] * 20)
    assert ci == (0.0, 0.0)


# ── build_findings gating ────────────────────────────────────────────────


def _base_day() -> dict:
    return {
        "mood": None,
        "energy": None,
        "steps": None,
        "weight": None,
        "kcal": None,
        "sleep": None,
        "habits": set(),
        "missed": set(),
        "text": "",
    }


def test_marginal_n_is_suppressed_even_with_a_striking_delta():
    """Regression for the actual bug: a habit with n=12/13 done-days (well
    under MIN_N_ADVICE) must never reach verdict='report', no matter how big
    the mood delta looks — this is exactly what got narrated as a real
    finding before the gate existed."""
    days = {}
    for i in range(12):
        d = _base_day()
        d["mood"] = 5.0
        d["habits"] = {"Writing output"}
        days[f"2026-01-{i + 1:02d}"] = d
    for i in range(12, 20):
        d = _base_day()
        d["mood"] = 1.0
        days[f"2026-01-{i + 1:02d}"] = d

    findings = build_findings(days)
    f = next(f for f in findings if f.id == "habit:Writing output")
    assert f.verdict == "suppress"


def test_habit_delta_reports_only_with_enough_n_and_a_real_gap():
    days = {}
    for i in range(20):
        d = _base_day()
        d["mood"] = 5.0
        d["habits"] = {"Shacharit"}
        days[f"2026-01-{i + 1:02d}"] = d
    for i in range(20, 40):
        d = _base_day()
        d["mood"] = 1.0
        days[f"2026-02-{i - 19:02d}"] = d

    findings = build_findings(days)
    f = next(f for f in findings if f.id == "habit:Shacharit")
    assert f.verdict == "report"
    assert f.tag == "same_day_confounded"
    assert f.n == 20


def test_zero_data_metric_is_alerted_not_treated_as_a_null_finding():
    """sleep hrs ↔ mood with n=0 must never be handed to the LLM as 'no
    correlation found' — that's a logging gap, not a finding."""
    days = {f"2026-01-{i + 1:02d}": _base_day() for i in range(20)}
    for i, d in enumerate(days.values()):
        d["mood"] = 3.0 + (i % 2)
        d["energy"] = 2.0

    findings = build_findings(days)
    f = next(f for f in findings if f.id == "corr:sleep_mood")
    assert f.verdict == "alert"
    assert f.tag == "data_pipeline"


def test_habit_below_min_n_is_suppressed_not_dropped():
    """Under-n habits still show up as verdict='suppress' (never 'report') —
    kept around so they can appear in the 'still thin on' gap list rather
    than vanishing without a trace."""
    days = {}
    for i in range(5):
        d = _base_day()
        d["mood"] = 5.0
        d["habits"] = {"wash 5 dishes"}
        days[f"2026-01-{i + 1:02d}"] = d
    for i in range(5, 20):
        d = _base_day()
        d["mood"] = 3.0
        days[f"2026-01-{i + 1:02d}"] = d

    findings = build_findings(days)
    f = next(f for f in findings if f.id == "habit:wash 5 dishes")
    assert f.verdict == "suppress"


# ── citation validation ──────────────────────────────────────────────────


def test_validate_citations_keeps_only_resolved_ids():
    text = (
        "- Mood tracks energy tightly. [corr:mood_energy]\n"
        "- Habits correlate with weight loss somehow. [trend:habits_weight]\n"
        "- Weigh-ins are trending down. [trend:weight]\n"
        "- Just a header with no citation\n"
    )
    result = _validate_citations(text, {"corr:mood_energy", "trend:weight"})

    assert "[corr:mood_energy]" in result
    assert "[trend:weight]" in result
    assert "trend:habits_weight" not in result
    assert "Habits correlate with weight loss somehow" not in result
    assert "Just a header with no citation" not in result
    assert "2 uncited or unresolved claim(s) dropped" in result


def test_advise_skips_the_llm_when_nothing_survives_the_gate(monkeypatch):
    """If every finding suppresses or alerts, advise() must return the 'not
    enough data' message directly — never construct the model client for an
    empty (or entirely unreliable) findings set."""
    import anthropic

    class _BoomClient:
        def __init__(self, *a, **kw):
            raise AssertionError("anthropic client should not be constructed")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _BoomClient)
    findings = [
        Finding(
            "corr:x_y", "x ↔ y", "r=+0.10", 8, "correlational_same_session", "suppress"
        ),
        Finding(
            "corr:sleep_mood", "sleep ↔ mood", "no data", 0, "data_pipeline", "alert"
        ),
    ]

    result = asyncio.run(advise(findings))

    assert "Not enough data yet" in result
    assert str(MIN_N_ADVICE) in result

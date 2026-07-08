import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from db import Database
from hypotheses import Hypotheses
from text_router import _hypothesis_summary


@pytest.fixture
def hyp(tmp_path):
    return Hypotheses(Database(str(tmp_path / "ops.db")))


def test_add_and_open(hyp):
    """A logged hypothesis persists its fields and shows up in the open list."""
    hid = hyp.add(
        "Friday anxiety comes from Shabbat prep",
        restatement="Friday anxiety tracks Shabbat-prep load",
        confirm_if="anxiety rises with prep hours",
        falsify_if="calm on high-prep Fridays",
        metric_keys=["prep_hours"],
        follow_up_date="2026-07-22",
    )
    rows = hyp.open()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == hid
    assert r["restatement"] == "Friday anxiety tracks Shabbat-prep load"
    assert r["metric_keys"] == "prep_hours"
    assert r["status"] == "active"


def test_due_respects_date_and_status(hyp):
    """due() returns active hypotheses whose follow-up has arrived, and skips
    resolved ones — so the follow-up fires once, not forever."""
    past = hyp.add("past", follow_up_date="2026-07-01")
    future = hyp.add("future", follow_up_date="2026-12-01")
    hyp.add("no date")  # empty follow_up_date never comes due

    due_ids = [r["id"] for r in hyp.due(today="2026-07-08")]
    assert due_ids == [past]
    assert future not in due_ids

    # Once prompted/resolved it drops out of due().
    hyp.set_status(past, "prompted")
    assert hyp.due(today="2026-07-08") == []


def test_set_status_removes_from_open(hyp):
    """Resolving a hypothesis takes it out of the open list."""
    hid = hyp.add("h", follow_up_date="2026-07-22")
    hyp.set_status(hid, "confirmed")
    assert hyp.open() == []


def test_followup_report_pulls_metrics(hyp):
    """The follow-up report summarises the metric readings logged since the
    hypothesis was raised — the payoff of persisting the test."""
    db = hyp.db
    hid = hyp.add(
        "prep drives anxiety",
        restatement="anxiety tracks prep",
        confirm_if="rises with prep",
        falsify_if="calm high-prep",
        metric_keys=["prep_hours"],
        follow_up_date="2026-07-22",
        created="2026-07-01",
    )
    # Readings after the created date count; one before it must be ignored.
    db.insert_metric("2026-06-25T09:00", "2026-06-25", "prep_hours", "9")  # before
    db.insert_metric("2026-07-03T09:00", "2026-07-03", "prep_hours", "2")
    db.insert_metric("2026-07-10T09:00", "2026-07-10", "prep_hours", "4")

    report = hyp.followup_report(hyp.get(hid))
    assert "Hypothesis check-in" in report
    assert "anxiety tracks prep" in report
    # 2 readings (the pre-creation 9 excluded), latest 4, avg (2+4)/2 = 3.
    assert "prep_hours: 2 readings, latest 4 (avg 3)" in report


def test_followup_report_no_readings(hyp):
    """A metric with nothing logged yet reports plainly rather than crashing."""
    hid = hyp.add("h", metric_keys=["never_logged"], created="2026-07-01")
    report = hyp.followup_report(hyp.get(hid))
    assert "never_logged: no readings" in report


def test_summary_is_compact_and_escaped():
    """The logged-hypothesis reply is a terse test setup, and user/LLM text with
    HTML-special chars is escaped so parse_mode='HTML' doesn't break."""
    result = {
        "restatement": "R & D <focus>",
        "confirm_if": "X happens",
        "falsify_if": "Y happens",
        "metrics": [{"key": "prep_hours", "description": "log Fri"}],
        "habits": ["shacharit"],
        "follow_up_date": "2026-07-22",
    }
    out = _hypothesis_summary(result)
    assert "&amp;" in out and "&lt;focus&gt;" in out  # escaped
    assert "✅ Confirm: X happens" in out
    assert "metric: prep_hours" in out
    assert "👁 Watch: shacharit" in out
    assert "Check back" in out
    # Terse: no multi-paragraph narrative.
    assert out.count("\n") < 8

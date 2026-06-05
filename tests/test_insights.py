import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from insights import Insights


@pytest.fixture
def ledger(tmp_path):
    return Insights(str(tmp_path))


def test_merge_adds_new_items(ledger):
    summary = ledger.merge(
        new_items=[
            {"kind": "hypothesis", "text": "Friday anxiety comes from Shabbat prep"},
            {"kind": "idea", "text": "the system should learn from agenda edits"},
        ],
        recurrences=[],
        on_date=date(2026, 6, 1),
    )
    assert len(summary["added"]) == 2
    assert summary["total"] == 2
    items = ledger.load()["items"]
    assert {it["id"] for it in items} == {1, 2}
    assert items[0]["occurrences"] == ["2026-06-01"]
    assert items[0]["first_seen"] == items[0]["last_seen"] == "2026-06-01"


def test_recurrence_bumps_without_duplicating(ledger):
    ledger.merge(
        [{"kind": "concern", "text": "I keep missing Shacharit"}],
        [],
        on_date=date(2026, 6, 1),
    )
    summary = ledger.merge([], [{"id": 1}], on_date=date(2026, 6, 8))
    assert len(summary["recurred"]) == 1
    assert summary["total"] == 1  # no new item created
    item = ledger.load()["items"][0]
    assert item["occurrences"] == ["2026-06-01", "2026-06-08"]
    assert item["last_seen"] == "2026-06-08"
    assert item["first_seen"] == "2026-06-01"


def test_recurrence_is_idempotent_same_day(ledger):
    ledger.merge(
        [{"kind": "insight", "text": "I work best in the morning"}],
        [],
        on_date=date(2026, 6, 1),
    )
    # Re-running extraction the same day must not double-count the occurrence.
    ledger.merge([], [{"id": 1}], on_date=date(2026, 6, 1))
    assert ledger.load()["items"][0]["occurrences"] == ["2026-06-01"]


def test_merge_ignores_bad_input(ledger):
    summary = ledger.merge(
        new_items=[
            {"kind": "bogus", "text": "wrong kind"},  # invalid kind
            {"kind": "idea", "text": "   "},  # empty text
            {"kind": "idea", "text": "valid idea"},
        ],
        recurrences=[{"id": 999}],  # nonexistent id
        on_date=date(2026, 6, 1),
    )
    assert len(summary["added"]) == 1
    assert len(summary["recurred"]) == 0
    assert summary["total"] == 1


def test_format_for_prompt_shows_recurrence_count(ledger):
    ledger.merge(
        [{"kind": "hypothesis", "text": "Friday dread"}], [], on_date=date(2026, 6, 1)
    )
    ledger.merge([], [{"id": 1}], on_date=date(2026, 6, 8))
    out = ledger.format_for_prompt()
    assert "raised 2×" in out
    assert "2026-06-01→2026-06-08" in out
    assert "Friday dread" in out


def test_format_empty(ledger):
    assert ledger.format_for_prompt() == ""
    assert "No insights" in ledger.format_for_telegram()

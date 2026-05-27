import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from agenda import Agenda


@pytest.fixture
def agenda(tmp_path):
    return Agenda(str(tmp_path))


def test_load_empty(agenda):
    data = agenda.load()
    assert data == {"items": []}


def test_accept_items(agenda):
    items = agenda.accept_items(["Do laundry", "Review PRs"])
    assert len(items) == 2
    assert items[0]["text"] == "Do laundry"
    assert items[0]["status"] == "open"
    assert items[0]["source"] == "llm"


def test_accept_items_persists(agenda):
    agenda.accept_items(["Task A"])
    data = agenda.load()
    assert len(data["items"]) == 1
    assert data["items"][0]["text"] == "Task A"


def test_accept_items_appends(agenda):
    agenda.accept_items(["First"])
    agenda.accept_items(["Second"])
    data = agenda.load()
    assert len(data["items"]) == 2
    assert data["items"][1]["id"] == 1


def test_mark_status_done(agenda):
    agenda.accept_items(["Do something"])
    agenda.mark_status(0, "done")
    data = agenda.load()
    assert data["items"][0]["status"] == "done"


def test_mark_status_missed(agenda):
    agenda.accept_items(["Do something"])
    agenda.mark_status(0, "missed")
    assert agenda.load()["items"][0]["status"] == "missed"


def test_get_open_filters(agenda):
    agenda.accept_items(["Open task", "Closed task"])
    agenda.mark_status(1, "done")
    open_items = agenda.get_open()
    assert len(open_items) == 1
    assert open_items[0]["text"] == "Open task"


def test_edit_item(agenda):
    agenda.accept_items(["Old text"])
    old = agenda.edit_item(0, "New text")
    assert agenda.load()["items"][0]["text"] == "New text"
    assert old == "Old text"


def test_edit_item_returns_none_for_missing_id(agenda):
    agenda.accept_items(["Something"])
    old = agenda.edit_item(99, "irrelevant")
    assert old is None


def test_accept_items_deduplicates(agenda):
    agenda.accept_items(["Do laundry", "Job search"])
    agenda.accept_items(["Job search", "Walk"])  # "Job search" already exists
    items = agenda.load()["items"]
    texts = [i["text"] for i in items]
    assert texts.count("Job search") == 1
    assert "Do laundry" in texts
    assert "Walk" in texts


def test_accept_items_dedup_case_insensitive(agenda):
    agenda.accept_items(["Anki review"])
    agenda.accept_items(["anki review"])  # same, different case
    assert len(agenda.load()["items"]) == 1


def test_accept_custom_source(agenda):
    items = agenda.accept_items(["Manual task"], source="manual")
    assert items[0]["source"] == "manual"


def test_edit_uses_position_not_id(agenda):
    # First batch completes, leaving non-zero IDs for second batch
    agenda.accept_items(["Old task A", "Old task B"])
    agenda.mark_status(0, "done")
    agenda.mark_status(1, "done")
    agenda.accept_items(["New task 1", "New task 2"])  # IDs will be 2 and 3

    open_items = agenda.get_open()
    actual_id = open_items[0]["id"]
    assert actual_id == 2  # not 0

    agenda.edit_item(actual_id, "Edited new task 1")
    assert agenda.get_open()[0]["text"] == "Edited new task 1"


def test_get_status_returns_all_items(agenda):
    agenda.accept_items(["Task A", "Task B", "Task C"])
    agenda.mark_status(0, "done")
    agenda.mark_status(1, "missed")
    all_items = agenda.get_status()
    assert len(all_items) == 3
    statuses = {i["text"]: i["status"] for i in all_items}
    assert statuses["Task A"] == "done"
    assert statuses["Task B"] == "missed"
    assert statuses["Task C"] == "open"


def test_get_status_empty(agenda):
    assert agenda.get_status() == []


def test_mark_status_uses_actual_id(agenda):
    agenda.accept_items(["First"])
    agenda.mark_status(0, "done")
    agenda.accept_items(["Second"])  # ID is 1, not 0

    open_items = agenda.get_open()
    assert open_items[0]["id"] == 1
    agenda.mark_status(open_items[0]["id"], "done")
    assert agenda.get_open() == []


def test_existing_summary_empty(agenda):
    assert agenda.existing_summary() == ""


def test_existing_summary_open_items(agenda):
    agenda.accept_items(["Task A", "Task B"])
    summary = agenda.existing_summary()
    assert "Still open" in summary
    assert "Task A" in summary
    assert "Task B" in summary


def test_existing_summary_mixed_status(agenda):
    agenda.accept_items(["Done task", "Open task"])
    agenda.mark_status(0, "done")
    summary = agenda.existing_summary()
    assert "Already completed/missed" in summary
    assert "Done task" in summary
    assert "Still open" in summary
    assert "Open task" in summary


@pytest.mark.asyncio
async def test_generate_calls_planner(agenda):
    mock_planner = AsyncMock()
    mock_planner.propose.return_value = ["Item 1", "Item 2"]
    result = await agenda.generate(mock_planner, calendar_events="10:00 standup")
    mock_planner.propose.assert_called_once_with("10:00 standup", "")
    assert result == ["Item 1", "Item 2"]


@pytest.mark.asyncio
async def test_generate_passes_existing_summary(agenda):
    agenda.accept_items(["Existing open task"])
    mock_planner = AsyncMock()
    mock_planner.propose.return_value = []
    await agenda.generate(mock_planner)
    _, summary_arg = mock_planner.propose.call_args[0]
    assert "Existing open task" in summary_arg

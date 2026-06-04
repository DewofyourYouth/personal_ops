"""Tests for /status command formatting (_status_message + Agenda.get_status)."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Patch required env vars before importing bot
_env = {"OPS_BOT_TOKEN": "fake", "OPS_CHAT_ID": "12345"}

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from agenda_handlers import AgendaHandlers
from bot_constants import STATUS_ICONS
from agenda import Agenda

_status_message = AgendaHandlers._status_message


@pytest.fixture
def agenda(tmp_path):
    return Agenda(str(tmp_path))


def test_status_message_shows_all_statuses():
    items = [
        {"text": "Deep work block", "status": "done"},
        {"text": "Strength training", "status": "missed"},
        {"text": "Language exchange", "status": "open"},
    ]
    msg = _status_message(items)
    assert "Deep work block" in msg
    assert "Strength training" in msg
    assert "Language exchange" in msg
    assert STATUS_ICONS["done"] in msg
    assert STATUS_ICONS["missed"] in msg
    assert STATUS_ICONS["open"] in msg


def test_status_message_numbering():
    items = [
        {"text": "First", "status": "done"},
        {"text": "Second", "status": "open"},
    ]
    msg = _status_message(items)
    assert "1." in msg
    assert "2." in msg


def test_status_message_header():
    msg = _status_message([{"text": "Something", "status": "open"}])
    assert "Agenda Status" in msg


def test_status_message_empty_list():
    # _status_message with no items — just the header
    msg = _status_message([])
    assert "Agenda Status" in msg


def test_get_status_after_mixed_marks(agenda):
    agenda.accept_items(["A", "B", "C"])
    agenda.mark_status(0, "done")
    agenda.mark_status(2, "missed")
    result = agenda.get_status()
    assert len(result) == 3
    by_text = {i["text"]: i["status"] for i in result}
    assert by_text["A"] == "done"
    assert by_text["B"] == "open"
    assert by_text["C"] == "missed"


def test_status_icons_all_present():
    assert "done" in STATUS_ICONS
    assert "missed" in STATUS_ICONS
    assert "open" in STATUS_ICONS

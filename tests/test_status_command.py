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
from status_handlers import StatusHandlers
from types import SimpleNamespace

_status_message = AgendaHandlers._status_message


@pytest.fixture
def agenda(tmp_path):
    return Agenda(str(tmp_path))


def test_status_message_shows_all_statuses():
    """The status message renders every agenda item with its status icon."""
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
    """Agenda status output numbers each rendered item."""
    items = [
        {"text": "First", "status": "done"},
        {"text": "Second", "status": "open"},
    ]
    msg = _status_message(items)
    assert "1." in msg
    assert "2." in msg


def test_status_message_header():
    """Agenda status output includes its header when items are present."""
    msg = _status_message([{"text": "Something", "status": "open"}])
    assert "Agenda Status" in msg


def test_status_message_empty_list():
    """Agenda status output still includes the header when there are no items."""
    # _status_message with no items — just the header
    msg = _status_message([])
    assert "Agenda Status" in msg


def test_get_status_after_mixed_marks(agenda):
    """Agenda.get_status reports done, open, and missed items together."""
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
    """The bot constants define icons for every agenda status value."""
    assert "done" in STATUS_ICONS
    assert "missed" in STATUS_ICONS
    assert "open" in STATUS_ICONS


# --- /status snapshot assembly (StatusHandlers section rendering) ---


def _status_handlers(
    *, pending, agenda_status, shabbat=False, checklist=None, agenda_items=None
):
    """A StatusHandlers wired with fakes for the synchronous render methods.
    bot/gcal/planner aren't touched by the renderers, so they're None.
    `checklist` feeds the rich checkbox list; `agenda_items` the rich agenda."""
    sh = StatusHandlers(
        bot=None,
        agenda_feature=SimpleNamespace(
            status_text=lambda: agenda_status,
            status_items=lambda: agenda_items or [],
        ),
        gcal=None,
        planner=None,
        shabbat=SimpleNamespace(quiet_now=lambda: shabbat),
        allowed_user=1,
    )
    sh.habits = SimpleNamespace(
        pending_today=lambda: pending,
        today_checklist=lambda: checklist or [],
    )
    return sh


def test_habits_section_lists_open_habits():
    sh = _status_handlers(pending=["Shacharit", "Strength"], agenda_status=None)
    section = sh._habits_section()
    assert "Open Habits (2)" in section
    assert "Shacharit" in section
    assert "Strength" in section


def test_habits_section_all_done():
    sh = _status_handlers(pending=[], agenda_status=None)
    assert "accounted for" in sh._habits_section()


def test_habits_section_shabbat_suppressed():
    sh = _status_handlers(pending=["Shacharit"], agenda_status=None, shabbat=True)
    section = sh._habits_section()
    assert "Shabbat" in section
    assert "Shacharit" not in section


def test_agenda_section_empty_prompts_plan():
    sh = _status_handlers(pending=[], agenda_status=None)
    assert "/plan" in sh._agenda_section()


def test_snapshot_message_combines_all_sections():
    sh = _status_handlers(
        pending=["Walk"], agenda_status="Agenda Status:\n1. ⌛ Ship it"
    )
    msg = sh._snapshot_message("• 15:00 — Dentist")
    assert "Status" in msg  # header
    assert "Open Habits" in msg
    assert "Walk" in msg
    assert "Ship it" in msg
    assert "Dentist" in msg


# --- Rich-message snapshot (checkbox list) ---


def test_rich_habits_checkbox_states():
    """Done habits render a checked box; open habits an unchecked one."""
    sh = _status_handlers(
        pending=[],
        agenda_status=None,
        checklist=[("Shacharit", True), ("Strength training", False)],
    )
    out = sh._rich_habits_html()
    assert '<input type="checkbox" checked>Shacharit</li>' in out
    assert '<input type="checkbox">Strength training</li>' in out
    assert out.count("<li>") == 2
    assert "Open Habits (1)" in out  # one still open


def test_rich_habits_all_done_header():
    sh = _status_handlers(
        pending=[], agenda_status=None, checklist=[("Shacharit", True)]
    )
    out = sh._rich_habits_html()
    assert "All habits done today" in out
    assert '<input type="checkbox" checked>' in out


def test_rich_habits_shabbat_has_no_checkboxes():
    sh = _status_handlers(
        pending=[], agenda_status=None, shabbat=True, checklist=[("Shacharit", False)]
    )
    out = sh._rich_habits_html()
    assert "Shabbat" in out
    assert "checkbox" not in out


def test_rich_habits_escapes_names():
    sh = _status_handlers(
        pending=[], agenda_status=None, checklist=[("Read <Tanakh> & pray", False)]
    )
    out = sh._rich_habits_html()
    assert "&lt;Tanakh&gt; &amp; pray" in out
    assert "<Tanakh>" not in out


def test_rich_agenda_is_a_table_with_icons():
    sh = _status_handlers(
        pending=[],
        agenda_status=None,
        agenda_items=[
            {"text": "Ship it", "status": "done"},
            {"text": "Call Galai", "status": "open"},
        ],
    )
    out = sh._rich_agenda_html()
    assert "<table>" in out and out.count("<tr>") == 3  # header + 2 rows
    assert STATUS_ICONS["done"] in out and STATUS_ICONS["open"] in out
    assert "Ship it" in out and "Call Galai" in out


def test_rich_agenda_empty_prompts_plan_without_table():
    sh = _status_handlers(pending=[], agenda_status=None, agenda_items=[])
    out = sh._rich_agenda_html()
    assert "/plan" in out
    assert "<table>" not in out


def test_rich_events_table():
    sh = _status_handlers(pending=[], agenda_status=None)
    out = sh._rich_events_html([("15:00", "Dentist"), ("All day", "Fast")], "")
    assert "<table>" in out
    assert '<th align="left">Time</th>' in out
    assert "Dentist" in out and "Fast" in out
    assert out.count("<tr>") == 3  # header + 2 events


def test_rich_events_empty_shows_note_not_table():
    sh = _status_handlers(pending=[], agenda_status=None)
    out = sh._rich_events_html([], "Calendar unavailable.")
    assert "Calendar unavailable." in out
    assert "<table>" not in out


def test_rich_events_escapes_summary():
    sh = _status_handlers(pending=[], agenda_status=None)
    out = sh._rich_events_html([("10:00", "Lunch w/ <Avi> & co")], "")
    assert "&lt;Avi&gt; &amp; co" in out
    assert "<Avi>" not in out


def test_rich_snapshot_combines_sections():
    sh = _status_handlers(
        pending=[],
        agenda_status=None,
        checklist=[("Walk", False)],
        agenda_items=[{"text": "Ship it", "status": "open"}],
    )
    out = sh._rich_snapshot_html([("15:00", "Dentist")], "")
    assert "📊 Status" in out
    assert '<input type="checkbox">Walk' in out
    assert "Ship it" in out
    assert "Dentist" in out
    assert out.count("<table>") == 2  # agenda + events

"""Tests for checkin phrase detection in _CHECKIN_RE."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

_env = {"OPS_BOT_TOKEN": "fake", "OPS_CHAT_ID": "12345"}

with patch.dict(os.environ, _env):
    sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
    from text_router import _CHECKIN_RE


def checkin_tag(text: str) -> tuple[str, str]:
    """Replicate the bot's checkin detection logic. Returns (tag, content)."""
    m = _CHECKIN_RE.match(text)
    if m:
        return "checkin", (m.group(1) or "").strip()
    return "log", text


# --- phrases that SHOULD match as checkin ---


def test_checkin_exact():
    """The bare 'checkin' command is classified as a checkin."""
    tag, _ = checkin_tag("checkin")
    assert tag == "checkin"


def test_checking_in_with_content():
    """A 'checking in' prefix captures the following activity text."""
    tag, content = checkin_tag("checking in, working on the tests")
    assert tag == "checkin"
    assert content == "working on the tests"


def test_check_in_colon():
    """A 'check in:' prefix captures content after the colon."""
    tag, content = checkin_tag("check in: reviewing PRs")
    assert tag == "checkin"
    assert content == "reviewing PRs"


def test_checking_in_bare():
    """The bare 'checking in' phrase is a checkin with empty content."""
    tag, content = checkin_tag("checking in")
    assert tag == "checkin"
    assert content == ""


def test_update_with_content():
    """An 'update' prefix captures activity text after punctuation."""
    tag, content = checkin_tag("update - deep work on Haki")
    assert tag == "checkin"
    assert content == "deep work on Haki"


def test_update_bare():
    """The bare 'update' phrase is treated as a checkin."""
    tag, _ = checkin_tag("update")
    assert tag == "checkin"


def test_status_update():
    """A 'status update' prefix captures the following status text."""
    tag, content = checkin_tag("status update, still on the same task")
    assert tag == "checkin"
    assert content == "still on the same task"


def test_status_bare():
    """The bare 'status update' phrase is treated as a checkin."""
    tag, _ = checkin_tag("status update")
    assert tag == "checkin"


def test_case_insensitive():
    """Checkin phrase matching is case-insensitive."""
    tag, _ = checkin_tag("Checking In, something")
    assert tag == "checkin"


def test_checking_in_no_punctuation():
    """Voice-note style 'checking in' without punctuation still captures content."""
    # voice note: Whisper may produce no comma
    tag, content = checkin_tag("checking in just working on stuff")
    assert tag == "checkin"
    assert content == "just working on stuff"


# --- phrases that should NOT match ---


def test_normal_note_not_checkin():
    """The note prefix is not swallowed by the checkin matcher."""
    tag, _ = checkin_tag("note: something interesting")
    assert tag == "log"  # PREFIXES loop handles this, not _CHECKIN_RE


def test_random_sentence_not_checkin():
    """A sentence containing an updated verb form is not a checkin command."""
    tag, _ = checkin_tag("I updated the docs")
    assert tag == "log"


def test_update_mid_sentence_not_checkin():
    """The word update in the middle of a sentence does not trigger checkin mode."""
    tag, _ = checkin_tag("please update the calendar")
    assert tag == "log"

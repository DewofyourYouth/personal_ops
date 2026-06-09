"""Tests for feedback/question phrase detection in _FEEDBACK_RE."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

_env = {"OPS_BOT_TOKEN": "fake", "OPS_CHAT_ID": "12345"}

with patch.dict(os.environ, _env):
    sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
    from text_router import _FEEDBACK_RE


def match(text: str) -> tuple[str, str]:
    m = _FEEDBACK_RE.match(text)
    if m:
        return "feedback", (m.group(1) or "").strip()
    return "log", text


# --- phrases that SHOULD match ---


def test_feedback_colon():
    """A 'feedback:' prefix captures the feedback request text."""
    tag, content = match("feedback: should I apply to this company?")
    assert tag == "feedback"
    assert content == "should I apply to this company?"


def test_feedback_bare():
    """The bare 'feedback' phrase enters feedback mode with empty content."""
    tag, _ = match("feedback")
    assert tag == "feedback"


def test_feedback_request():
    """A 'feedback request' prefix captures content after punctuation."""
    tag, content = match("feedback request, thinking about pivoting to consulting")
    assert tag == "feedback"
    assert content == "thinking about pivoting to consulting"


def test_feedback_request_colon():
    """A 'feedback request:' prefix captures content after the colon."""
    tag, content = match("feedback request: is this a good idea?")
    assert tag == "feedback"
    assert content == "is this a good idea?"


def test_question_colon():
    """A 'question:' prefix routes the message to feedback mode."""
    tag, content = match("question: does it make sense to learn Rust right now?")
    assert tag == "feedback"
    assert content == "does it make sense to learn Rust right now?"


def test_question_bare():
    """The bare 'question' phrase enters feedback mode with empty content."""
    tag, _ = match("question")
    assert tag == "feedback"


def test_i_have_a_question():
    """'I have a question' is accepted as a feedback request prefix."""
    tag, content = match("I have a question, should I pursue this lead?")
    assert tag == "feedback"
    assert content == "should I pursue this lead?"


def test_i_have_a_thought():
    """'I have a thought' is accepted and preserves the substantive content."""
    tag, content = match("I have a thought - what if I pitched Haki as a DevOps tool?")
    assert tag == "feedback"
    assert "DevOps tool" in content


def test_i_want_feedback():
    """'I want feedback' is accepted as an intentional feedback request."""
    tag, content = match("I want feedback on my CV approach")
    assert tag == "feedback"
    assert content == "on my CV approach"


def test_i_want_your_take():
    """'I want your take' is accepted as an intentional feedback request."""
    tag, content = match("I want your take on this consulting pitch")
    assert tag == "feedback"
    assert "consulting pitch" in content


def test_case_insensitive():
    """Feedback phrase matching is case-insensitive."""
    tag, _ = match("Feedback: some idea")
    assert tag == "feedback"


def test_multiline_voice():
    """Multiline voice transcripts still capture feedback content."""
    tag, content = match(
        "feedback request\nI've been thinking about cold outreach to DevOps leads"
    )
    assert tag == "feedback"
    assert "cold outreach" in content


# --- phrases that should NOT match ---


def test_normal_note_not_feedback():
    """The note prefix is not swallowed by the feedback matcher."""
    tag, _ = match("note: something interesting")
    assert tag == "log"


def test_random_sentence():
    """A sentence about giving feedback is not treated as a feedback command."""
    tag, _ = match("I gave feedback to my colleague")
    assert tag == "log"


def test_question_mid_sentence():
    """The word question in the middle of a sentence does not trigger feedback mode."""
    tag, _ = match("the question is whether to apply")
    assert tag == "log"

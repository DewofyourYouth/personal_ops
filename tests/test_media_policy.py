"""Tests for the sticker cooldown policy in media.py.

Stickers went from delight to spam because most call sites fired per-tap. The
call sites were curated, and `_should_send` is the central backstop that keeps
any kind from firing more often than its cooldown — these tests lock that in.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from media import _COOLDOWN_S, _DEFAULT_COOLDOWN_S, _should_send


def test_first_send_of_a_kind_is_allowed():
    assert _should_send("idea", 100.0, {})


def test_send_within_cooldown_is_suppressed():
    assert not _should_send("idea", 100.0 + 60, {"idea": 100.0})


def test_send_after_cooldown_is_allowed():
    assert _should_send("idea", 100.0 + _DEFAULT_COOLDOWN_S, {"idea": 100.0})


def test_streak_is_never_throttled():
    """Streak milestones (3/7/30/100/365) are rare by construction — two in one
    session (two different habits hitting a milestone) must both celebrate."""
    assert _should_send("streak", 100.0, {"streak": 100.0})


def test_done_uses_its_shorter_cooldown():
    done_cd = _COOLDOWN_S["done"]
    assert not _should_send("done", 100.0 + done_cd - 1, {"done": 100.0})
    assert _should_send("done", 100.0 + done_cd, {"done": 100.0})


def test_cooldowns_are_per_kind():
    """A recent 'plan' send must not suppress an 'idea' send."""
    assert _should_send("idea", 100.0 + 1, {"plan": 100.0})

"""Tests for the deterministic (pre-LLM) classification layer in text_router.

Classification is load-bearing: tags drive habit streaks, food macros, burnout
detection, and (via `#directive`) agenda weighting. These tests lock in the rules
that must NOT silently regress:

- A `#directive` is *declared*, never inferred — only an explicit `directive:`/
  `policy:` prefix produces it. This is the fix for the old `#values` "semantic
  magnet" that swallowed any first-person value statement.
- Personal/emotional value-laden statements must NOT be classified as directives by
  the deterministic layer; they fall through to `log` so the LLM can route them to
  checkin/insight.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from text_router import TextRouter

classify = TextRouter._classify_entry


def test_directive_prefix_is_declared():
    """`directive:` and `policy:` declare a #directive and strip the prefix."""
    assert classify("directive: more is not always better") == (
        "directive",
        "more is not always better",
    )
    assert classify("policy: don't launder the agenda through support language") == (
        "directive",
        "don't launder the agenda through support language",
    )


def test_value_laden_personal_statement_is_not_a_directive():
    """A first-person statement about what the user cares about is NOT a directive.

    This is the exact failure mode of the old `#values` tag: 'I care about my mother'
    is personal content, not an instruction to the system. The deterministic layer must
    leave it as `log` so the LLM routes it to checkin/insight — it must never become a
    directive without the explicit prefix.
    """
    for text in (
        "I care about my mother",
        "my family's financial situation matters to me",
        "I value being present with my kids",
    ):
        tag, _ = classify(text)
        assert tag != "directive", f"{text!r} should not be a directive"
        assert tag == "log", f"{text!r} should fall through to the LLM as 'log'"


def test_ambiguous_insight_or_checkin_falls_through_to_llm():
    """An entry that's ambiguous between insight and checkin is left for the LLM.

    The deterministic layer only fires on explicit prefixes; a bare reflective sentence
    returns 'log' so `classify_entry` (Haiku) can decide between insight and checkin.
    """
    tag, content = classify(
        "I notice I feel calmer on the days I walk before shul"
    )
    assert tag == "log"
    assert content == "I notice I feel calmer on the days I walk before shul"


def test_values_prefix_no_longer_recognized():
    """The retired `values:` prefix no longer produces a tag of its own.

    Regression guard: a message opening with `values:` must not resurrect the old
    magnet tag. It falls through as `log` (the leading word is just treated as text).
    """
    tag, _ = classify("values: I want to be more patient")
    assert tag != "values"
    assert tag != "directive"

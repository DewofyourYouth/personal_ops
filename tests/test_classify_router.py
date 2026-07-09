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
from context import Context
from habit_handlers import HabitStore, exact_habit_match
from logs import Logs
from text_router import (
    TextRouter,
    _AGENDA_DEST_RE,
    _extract_agenda_item,
    _is_nutrition_breakdown,
    _parse_metric_body,
)

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
    tag, content = classify("I notice I feel calmer on the days I walk before shul")
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


# --- Rules-first pass: nutrition, habit, metric (no LLM call) ---


def test_structured_nutrition_routes_to_food_without_llm():
    """An entry with an explicit calorie + macro breakdown is tagged #food deterministically."""
    tag, content = classify("chicken bowl — 550 kcal, 40g protein")
    assert tag == "food"
    assert content == "chicken bowl — 550 kcal, 40g protein"
    # The natural-language phrasing that had been leaking into #log now routes to food.
    assert (
        classify(
            "drinking a protein-enhanced coffee, 25 grams of protein and 130 calories"
        )[0]
        == "food"
    )


def test_calorie_mention_without_macros_is_not_food():
    """A calorie figure alone (no macro grams) is not enough to force #food.

    'burned 500 calories on my walk' is a checkin, not a meal — it must fall through.
    """
    assert classify("burned 500 calories on my walk today")[0] == "log"


def test_metric_parser_handles_plural_and_possessive():
    """Regression: 'metrics:' (plural) and a possessive filler word used to drop to #log,
    silently losing the reading. Key/value in either order must also parse."""
    assert _parse_metric_body("weight 92.9") == ("weight", 92.9, "", "92.9")
    assert _parse_metric_body("steps 12779") == ("steps", 12779.0, "", "12779")
    assert _parse_metric_body("8000 steps") == ("steps", 8000.0, "", "8000")
    assert _parse_metric_body("yesterday's steps 7095") == ("steps", 7095.0, "", "7095")


def test_metric_body_requires_a_number():
    """No numeric value → not a parseable metric (caller falls through to normal logging)."""
    assert _parse_metric_body("feeling good") is None


def test_nutrition_breakdown_predicate():
    assert _is_nutrition_breakdown("550 kcal, 40g protein")
    assert not _is_nutrition_breakdown("I feel tired today")


# --- Explicit agenda destination ("... to my agenda") ---


def test_agenda_destination_is_detected_and_item_extracted():
    """Regression: a stated destination ('to my agenda') used to be discarded by the
    classifier, which tagged the utterance #task and dropped it so it never reached
    /agenda. The rules-first match must fire and pull out the item text."""
    u = "Add goal reflection to my agenda and it should include putting it in personal ops."
    assert _AGENDA_DEST_RE.search(u.lower())
    assert _extract_agenda_item(u) == "goal reflection"

    for text, item in [
        ("put the dentist call on the agenda", "the dentist call"),
        ("add finish the deck to my agenda", "finish the deck"),
        ("note buy milk on my agenda", "buy milk"),
    ]:
        assert _AGENDA_DEST_RE.search(text.lower()), text
        assert _extract_agenda_item(text) == item, text


def test_agenda_destination_does_not_fire_without_the_phrase():
    """The phrase must be an explicit destination — an ordinary mention of the word
    'agenda' elsewhere, or none at all, must not trigger agenda routing."""
    assert not _AGENDA_DEST_RE.search("the meeting agenda was long")
    assert not _AGENDA_DEST_RE.search("add milk to the shopping list")


def test_exact_habit_match_is_conservative(tmp_path):
    """A bare known-habit string resolves to the canonical name (no LLM); a sentence that
    merely contains the words does not — avoids false positives in the classifier."""
    store = HabitStore(Logs(str(tmp_path)).db, Context(tmp_path))
    store.add("Daily walk")
    db = store.db
    assert exact_habit_match("daily walk", db) == "Daily walk"
    assert exact_habit_match("Daily Walk", db) == "Daily walk"
    assert exact_habit_match("I should do my daily walk later", db) is None
    assert exact_habit_match("some unrelated note", db) is None

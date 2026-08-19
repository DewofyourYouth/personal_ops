"""Tests for adding/removing tracked habits — HabitStore.remove_by_name plus the
HabitHandlers.add_habit_from_text / remove_habit_by_text wrappers that /addhabit
and the natural-language "add X habit" / "remove habit X" router intercept
(text_router.py) both call. Regex-level extraction of the phrasing itself is
covered in test_classify_router.py; this file covers what happens once a name
has been extracted.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context
from habit_handlers import HabitHandlers, HabitStore
from logs import Logs


def _store(tmp_path) -> HabitStore:
    return HabitStore(Logs(str(tmp_path)).db, Context(tmp_path))


def _handlers(tmp_path) -> HabitHandlers:
    """A HabitHandlers wired to a real store, no Telegram bot needed for the
    text-parsing methods under test."""
    h = HabitHandlers.__new__(HabitHandlers)
    h.logs = Logs(str(tmp_path))
    h.context = Context(tmp_path)
    h.store = HabitStore(h.logs.db, h.context)
    return h


# --- HabitStore.remove_by_name ---


def test_remove_by_name_deletes_the_habit(tmp_path):
    store = _store(tmp_path)
    store.add("Stretch")
    matched = store.remove_by_name("Stretch")
    assert matched == "Stretch"
    assert store.list_habits(tracked_only=False) == []


def test_remove_by_name_matches_forgivingly_typed_names(tmp_path):
    """Same forgiving match as pause/resume — case and stray whitespace don't
    block a removal."""
    store = _store(tmp_path)
    store.add("Eat at least 100 grams of protein.")
    matched = store.remove_by_name("eat at least 100 grams of protein")
    assert matched == "Eat at least 100 grams of protein."
    assert store.list_habits(tracked_only=False) == []


def test_remove_by_name_returns_none_when_no_match(tmp_path):
    store = _store(tmp_path)
    store.add("Stretch")
    assert store.remove_by_name("meditation") is None
    # the unrelated habit is untouched
    assert len(store.list_habits(tracked_only=False)) == 1


def test_remove_by_name_only_deletes_the_matched_habit(tmp_path):
    store = _store(tmp_path)
    store.add("Stretch")
    store.add("Meditate")
    store.remove_by_name("Stretch")
    remaining = [h["name"] for h in store.list_habits(tracked_only=False)]
    assert remaining == ["Meditate"]


# --- HabitHandlers.add_habit_from_text ---


def test_add_habit_from_text_creates_a_tracked_daily_habit(tmp_path):
    h = _handlers(tmp_path)
    added = h.add_habit_from_text("Cold shower")
    assert added == "Cold shower"
    habit = h.store.list_habits(tracked_only=False)[0]
    assert habit["name"] == "Cold shower"
    assert habit["tracked"] is True
    assert habit["days"] is None  # no [days] tag => every day


def test_add_habit_from_text_parses_trailing_day_tag(tmp_path):
    h = _handlers(tmp_path)
    added = h.add_habit_from_text("Stretch [mon,wed,fri]")
    assert added == "Stretch"
    habit = h.store.list_habits(tracked_only=False)[0]
    assert habit["name"] == "Stretch"
    assert habit["days"] == [0, 2, 4]


# --- HabitHandlers.remove_habit_by_text ---


@pytest.mark.asyncio
async def test_remove_habit_by_text_removes_a_matching_habit(tmp_path):
    h = _handlers(tmp_path)
    h.store.add("Stretch")
    removed = await h.remove_habit_by_text("stretch")
    assert removed == "Stretch"
    assert h.store.list_habits(tracked_only=False) == []


@pytest.mark.asyncio
async def test_remove_habit_by_text_returns_none_when_nothing_resolves(tmp_path):
    """No habits at all => the resolver has nothing to match, so no LLM call is
    made and the wrapper reports no match rather than raising."""
    h = _handlers(tmp_path)
    removed = await h.remove_habit_by_text("meditation")
    assert removed is None

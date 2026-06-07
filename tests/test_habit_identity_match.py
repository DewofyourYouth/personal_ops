"""Regression: /identity and /habitcue must match habit names a person actually types,
not require the stored name byte-for-byte. The trailing period on "Eat at least 100 grams
of protein." and the "(07:00–08:00)" annotation on "Shacharit (07:00–08:00)" both broke
the old exact match. (Transliterations like "Shachris" are handled by the LLM fallback,
which isn't unit-tested here.)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context
from habit_handlers import HabitStore, _match_key
from logs import Logs


def _store(tmp_path) -> HabitStore:
    store = HabitStore(Logs(str(tmp_path)).db, Context(tmp_path))
    store.add("Eat at least 100 grams of protein.")
    store.add("Shacharit (07:00–08:00)")
    return store


def test_match_key_normalises_punctuation_and_schedule():
    assert _match_key("Eat at least 100 grams of protein.") == (
        "eat at least 100 grams of protein"
    )
    assert _match_key("Shacharit (07:00–08:00)") == "shacharit"


def test_identity_matches_despite_trailing_period(tmp_path):
    store = _store(tmp_path)
    # Typed without the stored trailing period — used to fail.
    matched = store.set_identity_by_name("eat at least 100 grams of protein", "healthy")
    assert matched == "Eat at least 100 grams of protein."
    assert store.list_habits()[0]["identity"] == "healthy"


def test_cue_matches_despite_schedule_annotation(tmp_path):
    store = _store(tmp_path)
    # Typed without the "(07:00–08:00)" the stored name carries. The returned name is
    # the display form, which already drops the schedule annotation.
    matched = store.set_cue_by_name("shacharit", "after waking")
    assert matched == "Shacharit"


def test_unknown_name_still_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.set_identity_by_name("go to the gym", "athlete") is None

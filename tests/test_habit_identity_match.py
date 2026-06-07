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
    matched = store.add_identities("eat at least 100 grams of protein", ["healthy"])
    assert matched == "Eat at least 100 grams of protein."
    assert store.identities_of("Eat at least 100 grams of protein.") == ["healthy"]


def test_cue_matches_despite_schedule_annotation(tmp_path):
    store = _store(tmp_path)
    # Typed without the "(07:00–08:00)" the stored name carries. The returned name is
    # the display form, which already drops the schedule annotation.
    matched = store.set_cue_by_name("shacharit", "after waking")
    assert matched == "Shacharit"


def test_unknown_name_still_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.add_identities("go to the gym", ["athlete"]) is None
    assert store.remove_identity("go to the gym", "athlete") is None


# --- Many-to-many identity behaviour ---


PROTEIN = "eat at least 100 grams of protein"  # forgiving-matches the stored name


def test_identities_accumulate_and_dedupe(tmp_path):
    store = _store(tmp_path)
    store.add_identities(PROTEIN, ["healthy"])
    store.add_identities(PROTEIN, ["disciplined", "healthy"])  # re-add is idempotent
    assert store.identities_of(PROTEIN) == ["disciplined", "healthy"]  # sorted, no dup


def test_remove_one_identity_keeps_the_rest(tmp_path):
    store = _store(tmp_path)
    store.add_identities(PROTEIN, ["healthy", "disciplined"])
    store.remove_identity(PROTEIN, "Healthy")  # case-insensitive removal
    assert store.identities_of(PROTEIN) == ["disciplined"]


def test_identity_shared_across_habits(tmp_path):
    store = _store(tmp_path)
    store.add_identities(PROTEIN, ["healthy"])
    store.add_identities("shacharit", ["healthy"])
    healthy = [h["name"] for h in store.list_habits() if "healthy" in h["identities"]]
    assert len(healthy) == 2  # one identity, many habits


def test_denormalised_cache_tracks_join(tmp_path):
    # The dormant habits.identity column mirrors the join (comma-joined) so old string
    # readers keep working.
    store = _store(tmp_path)
    store.add_identities(PROTEIN, ["healthy", "disciplined"])
    cache = next(
        h["identity"]
        for h in store.list_habits()
        if h["name"] == "Eat at least 100 grams of protein."
    )
    assert cache == "disciplined, healthy"


def test_existing_single_identity_is_migrated_to_join(tmp_path):
    # A pre-M2M row with a single identity in the column is backfilled into the join
    # table when the store is (re)constructed.
    logs = Logs(str(tmp_path))
    store = HabitStore(logs.db, Context(tmp_path))
    store.add("Daf Yomi")
    logs.db.execute(
        "UPDATE habits SET identity = ? WHERE name = ?", ("Ben Torah", "Daf Yomi")
    )
    # Re-open: __init__ runs the idempotent migration.
    store2 = HabitStore(logs.db, Context(tmp_path))
    assert store2.identities_of("Daf Yomi") == ["Ben Torah"]

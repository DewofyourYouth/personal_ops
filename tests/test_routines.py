import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from db import Database
from routines import RoutineStore


@pytest.fixture
def store(tmp_path):
    return RoutineStore(Database(str(tmp_path / "ops.db")))


def test_upsert_and_get(store):
    """RoutineStore.upsert creates a routine retrievable case-insensitively."""
    store.upsert("Morning", ["22:30 lights out", "5:30 wake", "shul"], anchor="06:15")
    r = store.get("morning")  # case-insensitive
    assert r["name"] == "Morning"
    assert r["anchor"] == "06:15"
    assert r["steps"] == ["22:30 lights out", "5:30 wake", "shul"]


def test_upsert_replaces_not_duplicates(store):
    """Upserting an existing routine replaces its steps instead of duplicating it."""
    store.upsert("Morning", ["a", "b"])
    store.upsert("Morning", ["x", "y", "z"], anchor="06:15")  # reorder/edit = replace
    assert len(store.list()) == 1
    r = store.get("Morning")
    assert r["steps"] == ["x", "y", "z"]
    assert r["anchor"] == "06:15"


def test_blank_steps_are_dropped(store):
    """Blank routine steps are discarded before storage."""
    store.upsert("Morning", ["a", "  ", "", "b"])
    assert store.get("Morning")["steps"] == ["a", "b"]


def test_remove(store):
    """Removing a routine deletes it and reports whether anything was removed."""
    store.upsert("Morning", ["a"])
    assert store.remove("morning") is True
    assert store.get("Morning") is None
    assert store.remove("nope") is False


def test_insert_step(store):
    """RoutineStore.insert_step inserts a new step before the one-based position."""
    store.upsert("Morning", ["wake", "get dressed", "shul"])
    steps = store.insert_step("Morning", 2, "weigh myself")  # before "get dressed"
    assert steps == ["wake", "weigh myself", "get dressed", "shul"]
    assert store.get("Morning")["steps"] == steps


def test_insert_step_clamps_and_appends(store):
    """Step insertion clamps low positions, appends high positions, and handles misses."""
    store.upsert("Morning", ["a", "b"])
    store.insert_step("Morning", 99, "z")  # past end -> append
    store.insert_step("Morning", 0, "first")  # below 1 -> front
    assert store.get("Morning")["steps"] == ["first", "a", "b", "z"]
    assert store.insert_step("Nope", 1, "x") is None  # missing routine


def test_remove_step(store):
    """RoutineStore.remove_step removes by one-based position and returns the old step."""
    store.upsert("Morning", ["a", "b", "c"])
    assert store.remove_step("Morning", 2) == "b"
    assert store.get("Morning")["steps"] == ["a", "c"]
    assert store.remove_step("Morning", 9) is None  # out of range

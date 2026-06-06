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
    store.upsert("Morning", ["22:30 lights out", "5:30 wake", "shul"], anchor="06:15")
    r = store.get("morning")  # case-insensitive
    assert r["name"] == "Morning"
    assert r["anchor"] == "06:15"
    assert r["steps"] == ["22:30 lights out", "5:30 wake", "shul"]


def test_upsert_replaces_not_duplicates(store):
    store.upsert("Morning", ["a", "b"])
    store.upsert("Morning", ["x", "y", "z"], anchor="06:15")  # reorder/edit = replace
    assert len(store.list()) == 1
    r = store.get("Morning")
    assert r["steps"] == ["x", "y", "z"]
    assert r["anchor"] == "06:15"


def test_blank_steps_are_dropped(store):
    store.upsert("Morning", ["a", "  ", "", "b"])
    assert store.get("Morning")["steps"] == ["a", "b"]


def test_remove(store):
    store.upsert("Morning", ["a"])
    assert store.remove("morning") is True
    assert store.get("Morning") is None
    assert store.remove("nope") is False

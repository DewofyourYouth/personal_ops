import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import grocery
from db import Database
from grocery import (
    GroceryHandlers,
    GroceryStore,
    parse_grocery_items,
    split_grocery_items,
)


@pytest.fixture
def store(tmp_path):
    return GroceryStore(Database(str(tmp_path / "ops.db")))


@pytest.fixture
def handlers(tmp_path):
    logs = SimpleNamespace(db=Database(str(tmp_path / "ops.db")))
    return GroceryHandlers(bot=None, logs=logs, allowed_user=1)


class _Reply:
    """Captures the text/kwargs the voice handler sends back."""

    def __init__(self):
        self.calls = []

    async def __call__(self, msg, **kw):
        self.calls.append((msg, kw))


def test_split_grocery_items_handles_and_commas():
    """Grocery item splitting handles simple conjunctions and comma lists."""
    assert split_grocery_items("eggs, milk, and bread") == ["eggs", "milk", "bread"]


def test_parse_pick_up_phrase_splits_items():
    """Natural grocery pickup phrasing extracts separate item names."""
    assert parse_grocery_items("pick up eggs and milk at the grocery") == [
        "eggs",
        "milk",
    ]


def test_parse_add_to_grocery_list_phrase():
    """Add-to-list phrasing extracts grocery items without the destination words."""
    assert parse_grocery_items("add apples and bananas to the grocery list") == [
        "apples",
        "bananas",
    ]


def test_parse_non_grocery_text_returns_empty():
    """Non-grocery text is not captured by the grocery parser."""
    assert parse_grocery_items("pick up the thread from yesterday") == []


def test_add_items_lists_unchecked_first(store):
    """New grocery items are stored as unchecked checklist entries."""
    store.add_items(["eggs", "milk"])
    items = store.list()
    assert [i["text"] for i in items] == ["eggs", "milk"]
    assert [i["checked"] for i in items] == [False, False]


def test_toggle_marks_item_checked(store):
    """Toggling a grocery item flips its checked state."""
    store.add_items(["eggs"])
    item_id = store.list()[0]["id"]
    assert store.toggle(item_id)["checked"] is True
    assert store.toggle(item_id)["checked"] is False


def test_copy_text_includes_only_unchecked_items(store):
    """Copy text omits checked-off grocery items for sharing."""
    store.add_items(["eggs", "milk"])
    store.toggle(store.list()[0]["id"])
    assert store.copy_text() == "milk"


def test_add_existing_checked_item_reopens_it(store):
    """Adding an item that was already checked reopens it instead of duplicating it."""
    store.add_items(["eggs"])
    store.toggle(store.list()[0]["id"])
    store.add_items(["eggs"])
    items = store.list()
    assert len(items) == 1
    assert items[0]["text"] == "eggs"
    assert items[0]["checked"] is False


def test_clear_checked_removes_only_checked_items(store):
    """Clearing checked items leaves pending grocery items in place."""
    store.add_items(["eggs", "milk"])
    store.toggle(store.list()[0]["id"])
    assert store.clear_checked() == 1
    assert [i["text"] for i in store.list()] == ["milk"]


@pytest.mark.asyncio
async def test_voice_grocery_prefix_itemizes_and_adds(handlers, monkeypatch):
    """A 'grocery …' voice note itemizes the rest via the LLM and adds the items."""
    monkeypatch.setattr(
        grocery, "itemize_speech", AsyncMock(return_value=["eggs", "milk", "bread"])
    )
    reply = _Reply()

    handled = await handlers.handle_voice_text(
        "groceries I need to pick up some eggs milk and bread", reply
    )

    assert handled is True
    assert [i["text"] for i in handlers.store.list()] == ["eggs", "milk", "bread"]
    assert reply.calls and "Added: eggs, milk, bread" in reply.calls[0][0]


@pytest.mark.asyncio
async def test_voice_grocery_prefix_but_not_a_list_falls_back(handlers, monkeypatch):
    """A note that opens with 'grocery' but isn't a list falls back to a regular log."""
    monkeypatch.setattr(grocery, "itemize_speech", AsyncMock(return_value=None))
    reply = _Reply()

    handled = await handlers.handle_voice_text("grocery store was packed today", reply)

    assert handled is False
    assert handlers.store.list() == []
    assert reply.calls == []


@pytest.mark.asyncio
async def test_voice_without_grocery_prefix_is_ignored(handlers):
    """Voice notes that don't start with grocery/groceries are left for the log."""
    handled = await handlers.handle_voice_text("remind me to call the dentist", None)
    assert handled is False


@pytest.mark.asyncio
async def test_voice_pick_up_at_the_store_phrase_is_captured(handlers):
    """A spoken 'pick up X at the store' is caught deterministically, same as typed
    text — regression test for a voice note that fell through to the general
    classifier (and got mis-tagged as #checkin) because only the literal
    'grocery'/'groceries' prefix was checked for voice notes."""
    reply = _Reply()

    handled = await handlers.handle_voice_text("pick up lemons at the store", reply)

    assert handled is True
    assert [i["text"] for i in handlers.store.list()] == ["lemons"]
    assert reply.calls and "Added: lemons" in reply.calls[0][0]


@pytest.mark.asyncio
async def test_voice_grocery_falls_back_to_splitter_on_llm_error(handlers, monkeypatch):
    """If the LLM call fails, the deterministic splitter still captures the items."""
    monkeypatch.setattr(
        grocery, "itemize_speech", AsyncMock(side_effect=RuntimeError("api down"))
    )
    reply = _Reply()

    handled = await handlers.handle_voice_text("grocery eggs, milk", reply)

    assert handled is True
    assert [i["text"] for i in handlers.store.list()] == ["eggs", "milk"]

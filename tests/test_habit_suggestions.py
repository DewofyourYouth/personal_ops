"""Tests for HabitHandlers.handle_suggestion — accepting/rejecting a weekly
habit suggestion.

Regression coverage for a real bug: when the habit named in a suggestion no
longer resolves (renamed/archived/removed between when the suggestion was
generated and when the user taps Accept), set_cue/set_days/rename/archive
used to fall back to the stale name (`matched or habit`), show a false
"✅ done" message, and *still* unconditionally mark the suggestion
"accepted" — even though nothing was actually changed. The fix: only mark a
suggestion accepted when the underlying action actually found a match.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context
from habit_handlers import HabitHandlers, HabitStore
from logs import Logs


def _handlers(tmp_path) -> HabitHandlers:
    h = HabitHandlers.__new__(HabitHandlers)
    h.logs = Logs(str(tmp_path))
    h.context = Context(tmp_path)
    h.store = HabitStore(h.logs.db, h.context)
    return h


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = types.SimpleNamespace(id=1)
        self.edited = None

    async def answer(self, text=""):
        pass

    async def edit_message_text(self, text, parse_mode=None):
        self.edited = text


def _accept(h: HabitHandlers, sid: int) -> _FakeQuery:
    query = _FakeQuery(f"hbs_accept:{sid}")
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(h.handle_suggestion(update, None))
    return query


def test_accept_set_cue_succeeds_when_habit_exists(tmp_path):
    h = _handlers(tmp_path)
    h.store.add("Stretch")
    sid = h.store.save_suggestion(
        "Stretch", "Stretch: set a cue", "set_cue", {"cue": "after coffee"}
    )
    query = _accept(h, sid)
    assert "✅ Cue set" in query.edited
    assert h.store.get_suggestion(sid)["status"] == "accepted"


def test_accept_set_cue_fails_cleanly_when_habit_no_longer_exists(tmp_path):
    """The habit was renamed/removed since the suggestion was generated —
    set_cue_by_name returns None. Must NOT show a false success message and
    must NOT mark the suggestion accepted (nothing actually changed)."""
    h = _handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Old Habit Name",
        "Old Habit Name: set a cue",
        "set_cue",
        {"cue": "after coffee"},
    )
    query = _accept(h, sid)
    assert "✅" not in query.edited
    assert "not found" in query.edited.lower()
    assert h.store.get_suggestion(sid)["status"] == "pending"


def test_accept_set_days_fails_cleanly_when_habit_no_longer_exists(tmp_path):
    h = _handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Old Habit Name", "Old Habit Name: set days", "set_days", {"days": [0, 2, 4]}
    )
    query = _accept(h, sid)
    assert "✅" not in query.edited
    assert "not found" in query.edited.lower()
    assert h.store.get_suggestion(sid)["status"] == "pending"


def test_accept_rename_fails_cleanly_when_habit_no_longer_exists(tmp_path):
    h = _handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Old Habit Name", "Old Habit Name: rename", "rename", {"name": "New Name"}
    )
    query = _accept(h, sid)
    assert "✅" not in query.edited
    assert "not found" in query.edited.lower()
    assert h.store.get_suggestion(sid)["status"] == "pending"


def test_accept_archive_fails_cleanly_when_habit_no_longer_exists(tmp_path):
    """archive already showed the right message on a miss, but — since the
    status update sat outside the if/elif chain — it still marked the
    suggestion accepted even on this failure branch. Regression for that."""
    h = _handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Old Habit Name", "Old Habit Name: archive", "archive", {}
    )
    query = _accept(h, sid)
    assert "✅" not in query.edited
    assert "not found" in query.edited.lower()
    assert h.store.get_suggestion(sid)["status"] == "pending"


def test_accept_archive_succeeds_when_habit_exists(tmp_path):
    h = _handlers(tmp_path)
    h.store.add("Stretch")
    sid = h.store.save_suggestion("Stretch", "Stretch: archive", "archive", {})
    query = _accept(h, sid)
    assert "✅" in query.edited
    assert h.store.get_suggestion(sid)["status"] == "accepted"

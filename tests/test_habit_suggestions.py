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


# --- With Habitify as the source, accepting a rename/set_days/archive makes the
# change in Habitify (it used to refuse and say "do it in Habitify yourself"). ---

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from habitify import HabitifyError, HabitifyHabitSync  # noqa: E402


def _habitify_handlers(tmp_path) -> HabitHandlers:
    h = _handlers(tmp_path)
    h.store.add("Eat at least 100 grams of protein", habitify_id="protein-id")
    sync = MagicMock(spec=HabitifyHabitSync)
    sync.client = MagicMock()
    h.habitify_sync = sync
    h.refresh_habits_from_habitify = AsyncMock()
    return h


def test_accept_rename_shrinks_the_habit_in_habitify(tmp_path):
    h = _habitify_handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein",
        "Shrink it",
        "rename",
        {"name": "Protein with breakfast"},
    )

    query = _accept(h, sid)

    h.habitify_sync.client.update_habit.assert_called_once_with(
        "protein-id", {"name": "Protein with breakfast"}
    )
    h.refresh_habits_from_habitify.assert_awaited_once_with(force=True)
    assert "✅" in query.edited
    assert h.store.get_suggestion(sid)["status"] == "accepted"


def test_accept_set_days_converts_to_habitify_weekdays(tmp_path):
    h = _habitify_handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein", "Fewer days", "set_days", {"days": [0, 2]}
    )

    _accept(h, sid)

    # Local Monday=0 → Habitify Sunday=0.
    h.habitify_sync.client.update_habit.assert_called_once_with(
        "protein-id", {"occurrence": {"type": "weekDays", "days": [1, 3]}}
    )


def test_accept_archive_archives_in_habitify(tmp_path):
    h = _habitify_handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein", "Park it", "archive", {}
    )

    _accept(h, sid)

    h.habitify_sync.client.archive.assert_called_once_with("protein-id")


def test_habitify_rejecting_the_change_leaves_it_pending(tmp_path):
    h = _habitify_handlers(tmp_path)
    h.habitify_sync.client.update_habit.side_effect = HabitifyError(500, "boom")
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein",
        "Shrink it",
        "rename",
        {"name": "Protein with breakfast"},
    )

    query = _accept(h, sid)

    assert "✅" not in query.edited
    h.refresh_habits_from_habitify.assert_not_awaited()
    assert h.store.get_suggestion(sid)["status"] == "pending"


# --- Weekly job: only a few, and not the same habit again right away. ---


def test_weekly_suggestions_cap_and_skip_recently_suggested(tmp_path, monkeypatch):
    import habit_tracker
    from habit_handlers import _MAX_WEEKLY_SUGGESTIONS

    names = ["Water", "Protein", "Dishes", "Weigh in", "Lift"]
    monkeypatch.setattr(
        habit_tracker,
        "struggling_habits",
        lambda logs: [{"name": n, "rate": 0.1} for n in names],
    )
    h = _handlers(tmp_path)
    h.quiet_window = MagicMock()
    h.quiet_window.is_quiet_at.return_value = False
    h.planner = MagicMock()
    h.planner.habit_weekly_suggestions = AsyncMock(return_value=[])
    h.store.save_suggestion("protein", "last week's tip", "rename", {"name": "x"})

    asyncio.run(h.weekly_habit_suggestions())

    sent = [s["name"] for s in h.planner.habit_weekly_suggestions.call_args.args[0]]
    assert "Protein" not in sent  # suggested within the cooldown
    assert sent == ["Water", "Dishes", "Weigh in", "Lift"][:_MAX_WEEKLY_SUGGESTIONS]


# --- ✏️ Adjust: reply with your own version, and that's what gets applied. ---


def _tap_adjust(h: HabitHandlers, sid: int) -> _FakeQuery:
    query = _FakeQuery(f"hbs_adjust:{sid}")
    query.message = types.SimpleNamespace(chat_id=1)
    asyncio.run(h.handle_suggestion(types.SimpleNamespace(callback_query=query), None))
    return query


def _reply(h: HabitHandlers, text: str) -> tuple[bool, list[str]]:
    replies: list[str] = []

    async def reply_text(t, parse_mode=None):
        replies.append(t)

    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=1),
        message=types.SimpleNamespace(text=text, reply_text=reply_text),
    )
    return asyncio.run(h.try_handle_text(update, text)), replies


def test_adjusted_rename_applies_the_typed_version(tmp_path):
    h = _habitify_handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein", "Shrink", "rename", {"name": "LLM's idea"}
    )

    _tap_adjust(h, sid)
    consumed, replies = _reply(h, "Protein shake after Shacharit")

    assert consumed
    h.habitify_sync.client.update_habit.assert_called_once_with(
        "protein-id", {"name": "Protein shake after Shacharit"}
    )
    s = h.store.get_suggestion(sid)
    assert s["status"] == "accepted"
    assert s["value"] == {"name": "Protein shake after Shacharit"}  # what was applied


def test_adjusted_archive_becomes_a_rename(tmp_path):
    h = _habitify_handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein", "Park it", "archive", {}
    )

    _tap_adjust(h, sid)
    _reply(h, "Protein with breakfast")

    h.habitify_sync.client.archive.assert_not_called()
    h.habitify_sync.client.update_habit.assert_called_once_with(
        "protein-id", {"name": "Protein with breakfast"}
    )


def test_unreadable_days_keep_waiting_for_a_retry(tmp_path):
    h = _habitify_handlers(tmp_path)
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein", "Fewer", "set_days", {"days": [0]}
    )

    _tap_adjust(h, sid)
    _, replies = _reply(h, "whenever")
    assert "Couldn't read" in replies[0]
    _reply(h, "sun, tue thursday")

    h.habitify_sync.client.update_habit.assert_called_once_with(
        "protein-id", {"occurrence": {"type": "weekDays", "days": [0, 2, 4]}}
    )


def test_cancel_and_unrelated_text(tmp_path):
    h = _habitify_handlers(tmp_path)
    assert _reply(h, "just a normal message")[0] is False  # nothing pending
    sid = h.store.save_suggestion(
        "Eat at least 100 grams of protein", "Shrink", "rename", {"name": "x"}
    )
    _tap_adjust(h, sid)
    assert _reply(h, "/cancel")[0] is True
    h.habitify_sync.client.update_habit.assert_not_called()
    assert h.store.get_suggestion(sid)["status"] == "pending"

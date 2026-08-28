from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

from context import Context
from habit_handlers import HabitStore, exact_habit_match, match_habit
from habit_tracker import load_habit_logs
from logs import Logs


def _remote(
    habit_id: str,
    name: str,
    *,
    days=None,
    area: str | None = None,
    periodicity: str = "daily",
    goal: int = 1,
    archived: bool = False,
):
    occurrence = {"type": "daily"}
    if days is not None:
        occurrence = {"type": "weekDays", "days": days}
    return {
        "id": habit_id,
        "name": name,
        "type": "good",
        "isArchived": archived,
        "occurrence": occurrence,
        "goals": [
            {
                "periodicity": periodicity,
                "value": goal,
                "unit": "rep",
                "isActive": True,
            }
        ],
        "areas": [{"name": area}] if area else [],
        "timeOfDays": [],
    }


def _store(tmp_path):
    logs = Logs(str(tmp_path))
    return logs, HabitStore(logs.db, Context(tmp_path))


def test_habitify_projection_imports_active_habits_and_schedule(tmp_path):
    _, store = _store(tmp_path)

    result = store.sync_from_habitify(
        [_remote("workout-id", "Workout", days=[0, 2, 4], area="Health")]
    )

    habit = store.list_habits()[0]
    assert result == {"created": 1, "updated": 0, "active": 1}
    assert habit["name"] == "Workout"
    assert habit["habitify_id"] == "workout-id"
    assert habit["habitify_managed"] is True
    assert habit["section"] == "Health"
    # Habitify Sun/Tue/Thu -> Python Sun/Tue/Thu.
    assert habit["days"] == [1, 3, 6]


def test_remote_rename_preserves_old_name_as_streak_and_matching_alias(tmp_path):
    _, store = _store(tmp_path)
    store.add("Morning workout")
    store.set_cue_by_name("Morning workout", "after coffee")
    store.sync_from_habitify([_remote("workout-id", "Morning workout")])
    store.sync_from_habitify([_remote("workout-id", "Workout")])

    habit = store.list_habits()[0]
    assert habit["name"] == "Workout"
    assert habit["aliases"] == ["Morning workout"]
    assert habit["cue"] == "after coffee"  # Personal Ops-only metadata survives.
    assert exact_habit_match("Morning workout", store.db) == "Workout"


def test_migration_name_override_reuses_existing_local_row(tmp_path):
    _, store = _store(tmp_path)
    store.add("Daily walk (7000 steps minimum, includes walk to shul)")

    store.sync_from_habitify([_remote("steps-id", "Step Count")])

    habits = store.list_habits(tracked_only=False)
    assert len(habits) == 1
    assert habits[0]["name"] == "Step Count"
    assert habits[0]["habitify_id"] == "steps-id"
    assert habits[0]["aliases"] == [
        "Daily walk (7000 steps minimum, includes walk to shul)"
    ]


def test_missing_remote_habit_is_removed_from_active_projection(tmp_path):
    _, store = _store(tmp_path)
    store.sync_from_habitify([_remote("workout-id", "Workout")])
    store.sync_from_habitify([])

    assert store.list_habits() == []
    assert store.list_habits(tracked_only=False)[0]["tracked"] is False


def test_archived_habitify_definition_disables_matching_legacy_row(tmp_path):
    _, store = _store(tmp_path)
    store.add("Call Rebbe at 5:30 am.")

    store.sync_from_habitify(
        [_remote("rebbe-id", "Call Rebbe at 5:30 am.", archived=True)]
    )

    habit = store.list_habits(tracked_only=False)[0]
    assert habit["habitify_id"] == "rebbe-id"
    assert habit["habitify_managed"] is True
    assert habit["tracked"] is False


@pytest.mark.asyncio
async def test_free_text_uses_constrained_llm_against_habitify_projection(tmp_path):
    _, store = _store(tmp_path)
    store.sync_from_habitify([_remote("workout-id", "Workout")])
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={"habit": "Workout"})]
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    with patch("habit_handlers.anthropic.AsyncAnthropic", return_value=client):
        assert await match_habit("hit the gym", store.db) == "Workout"

    enum = client.messages.create.call_args.kwargs["tools"][0]["input_schema"]
    assert enum["properties"]["habit"]["enum"] == ["Workout", "none"]


def test_completion_projection_is_visible_to_streaks_and_reversible(tmp_path):
    logs, store = _store(tmp_path)
    store.sync_from_habitify([_remote("tefillin-id", "Tefillin")])
    from datetime import date

    target = date.today()
    store.sync_habitify_completions(
        target,
        [{"id": "tefillin-id", "name": "Tefillin"}],
        ["tefillin-id"],
    )
    assert load_habit_logs(logs)[target.isoformat()] == ["tefillin"]
    assert store.habitify_completion_entries(target) == [
        {"tag": "habit", "content": "Tefillin", "date": target.isoformat()}
    ]

    # Undoing in Habitify removes the projection on the next refresh.
    store.sync_habitify_completions(target, [], ["tefillin-id"])
    assert target.isoformat() not in load_habit_logs(logs)
    assert store.habitify_completion_entries(target) == []


def test_unresolved_habitify_status_keeps_last_known_projection(tmp_path):
    _, store = _store(tmp_path)
    from datetime import date

    target = date.today()
    store.sync_habitify_completions(
        target,
        [{"id": "core-id", "name": "Core Training"}],
        ["core-id"],
    )
    store.sync_habitify_completions(target, [], [])

    assert store.habitify_completion_entries(target) == [
        {"tag": "habit", "content": "Core Training", "date": target.isoformat()}
    ]

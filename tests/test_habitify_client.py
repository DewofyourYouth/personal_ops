import json
import urllib.error

import pytest

from ops import habitify
from ops.habitify import HabitifyClient, HabitifyError, HabitifyHabitSync


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_notes_requests_the_per_habit_endpoint_with_date_range(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse(
            json.dumps({"data": [{"id": "n1", "content": "hi"}]}).encode()
        )

    monkeypatch.setattr(habitify.urllib.request, "urlopen", fake_urlopen)
    client = HabitifyClient("test-key")

    result = client.notes("habit-1", start="2026-08-01", end="2026-08-30")

    assert result == [{"id": "n1", "content": "hi"}]
    assert captured["url"].startswith(
        "https://api.habitify.me/v2/habits/habit-1/notes?"
    )
    assert "from=2026-08-01" in captured["url"]
    assert "to=2026-08-30" in captured["url"]


def test_notes_omits_query_string_without_a_date_range(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse(json.dumps({"data": []}).encode())

    monkeypatch.setattr(habitify.urllib.request, "urlopen", fake_urlopen)
    client = HabitifyClient("test-key")

    assert client.notes("habit-1") == []
    assert captured["url"] == "https://api.habitify.me/v2/habits/habit-1/notes"


def test_create_post_is_not_retried_after_ambiguous_network_failure(monkeypatch):
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("lost response")

    monkeypatch.setattr(habitify.urllib.request, "urlopen", fail)
    client = HabitifyClient("test-key")

    with pytest.raises(HabitifyError):
        client.create_habit({"name": "Water", "type": "good"})

    assert calls == 1


def test_get_retries_transient_network_failures(monkeypatch):
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("temporary")

    monkeypatch.setattr(habitify.urllib.request, "urlopen", fail)
    monkeypatch.setattr(habitify.time, "sleep", lambda _: None)
    client = HabitifyClient("test-key")

    with pytest.raises(HabitifyError):
        client._request("GET", "/habits")

    assert calls == 5


def test_sync_maps_short_telegram_names_to_existing_habitify_names():
    class Client:
        def list_habits(self):
            return [
                {"id": "steps-id", "name": "Step Count"},
                {"id": "shacharit-id", "name": "Shacharit (07:00–08:00)"},
            ]

        def complete(self, habit_id, target_date):
            self.completed = (habit_id, target_date)

    client = Client()
    sync = HabitifyHabitSync(client)

    sync.complete("Daily walk", "2026-08-28")
    assert client.completed == ("steps-id", "2026-08-28")

    sync.complete("Shacharit", "2026-08-28")
    assert client.completed == ("shacharit-id", "2026-08-28")


def test_sync_treats_already_completed_conflict_as_success():
    class Client:
        def list_habits(self):
            return [{"id": "water-id", "name": "Drink water"}]

        def complete(self, habit_id, target_date):
            raise HabitifyError(409, "already completed")

    HabitifyHabitSync(Client()).complete("Drink water", "2026-08-28")


def test_sync_rejects_unknown_habit_without_posting():
    class Client:
        def list_habits(self):
            return [{"id": "water-id", "name": "Drink water"}]

        def complete(self, habit_id, target_date):
            raise AssertionError("should not post")

    with pytest.raises(HabitifyError, match="no active habit"):
        HabitifyHabitSync(Client()).complete("Unknown habit", "2026-08-28")


def test_completion_projection_handles_daily_and_weekly_goals():
    class Client:
        def list_habits(self):
            return [
                {
                    "id": "daily",
                    "name": "Tefillin",
                    "type": "good",
                    "isArchived": False,
                },
                {
                    "id": "weekly",
                    "name": "Core Training",
                    "type": "good",
                    "isArchived": False,
                },
            ]

        def journal(self, target_date):
            return [
                {
                    "id": "daily",
                    "status": "completed",
                    "progress": {"periodicity": "daily", "current": 1, "target": 1},
                },
                {
                    "id": "weekly",
                    "status": "inprogress",
                    "progress": {"periodicity": "weekly", "current": 2, "target": 3},
                },
            ]

        def statistics(self, habit_id, start, end):
            assert habit_id == "weekly"
            return {
                "dailyProgress": [
                    {"date": "2026-08-28", "totalLog": 1, "status": "inprogress"}
                ]
            }

    assert HabitifyHabitSync(Client()).completions_for_date("2026-08-28") == {
        "completed": [
            {"id": "daily", "name": "Tefillin"},
            {"id": "weekly", "name": "Core Training"},
        ],
        "failed": [],
        "resolved_ids": ["daily", "weekly"],
    }


def test_failed_weekly_read_is_left_unresolved():
    class Client:
        def list_habits(self):
            return [
                {
                    "id": "weekly",
                    "name": "Core Training",
                    "type": "good",
                    "isArchived": False,
                }
            ]

        def journal(self, target_date):
            return [
                {
                    "id": "weekly",
                    "status": "inprogress",
                    "progress": {"periodicity": "weekly", "current": 2, "target": 3},
                }
            ]

        def statistics(self, habit_id, start, end):
            raise HabitifyError(503, "temporary")

    assert HabitifyHabitSync(Client()).completions_for_date("2026-08-28") == {
        "completed": [],
        "failed": [],
        "resolved_ids": [],
    }


def test_completion_projection_surfaces_explicit_daily_failures():
    class Client:
        def list_habits(self):
            return [
                {"id": "daily", "name": "Tefillin", "type": "good", "isArchived": False}
            ]

        def journal(self, target_date):
            return [
                {
                    "id": "daily",
                    "status": "failed",
                    "progress": {"periodicity": "daily", "current": 0, "target": 1},
                }
            ]

    assert HabitifyHabitSync(Client()).completions_for_date("2026-08-28") == {
        "completed": [],
        "failed": [{"id": "daily", "name": "Tefillin"}],
        "resolved_ids": ["daily"],
    }

import urllib.error

import pytest

from ops import habitify
from ops.habitify import HabitifyClient, HabitifyError, HabitifyHabitSync


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

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from api.main import create_app
from logs import Logs


TOKEN = "a-long-random-test-token-32-chars!"


def _client(logs=None, habitify_sync=None):
    if logs is None:
        logs = MagicMock()
        logs.db.metric_exists.return_value = False
    habitify_sync = habitify_sync or MagicMock()
    return (
        TestClient(
            create_app(ingest_token=TOKEN, logs=logs, habitify_sync=habitify_sync)
        ),
        logs,
    )


def test_metric_requires_bearer_token():
    client, _ = _client()
    with client:
        response = client.post("/metrics", json={"key": "weight", "value": 91.2})
    assert response.status_code == 401


def test_api_refuses_to_start_without_ingest_token():
    with pytest.raises(RuntimeError, match="INGEST_TOKEN"):
        with TestClient(create_app(ingest_token="", logs=MagicMock())):
            pass


def test_weight_is_logged_at_apple_health_sample_time():
    client, logs = _client()
    payload = {
        "key": "weight",
        "value": 91.2,
        "unit": "kg",
        "recorded_at": "2026-08-28T05:42:10+03:00",
    }
    with client:
        response = client.post(
            "/metrics",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json()["duplicate"] is False
    when = logs.write_metric.call_args.kwargs["when"]
    assert when == datetime(2026, 8, 28, 5, 42, 10, tzinfo=ZoneInfo("Asia/Jerusalem"))
    logs.write_metric.assert_called_once_with("weight", 91.2, "kg", when=when)


def test_retry_of_same_sample_is_idempotent():
    client, logs = _client()
    logs.db.metric_exists.return_value = True
    with client:
        response = client.post(
            "/metrics",
            json={
                "key": "weight",
                "value": 91.2,
                "unit": "kg",
                "recorded_at": "2026-08-28T05:42:10+03:00",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    logs.write_metric.assert_not_called()


def test_naive_sample_timestamp_is_rejected():
    client, _ = _client()
    with client:
        response = client.post(
            "/metrics",
            json={
                "key": "weight",
                "value": 91.2,
                "recorded_at": "2026-08-28T05:42:10",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 422


def test_weight_also_completes_habitify_weigh_in():
    habitify_sync = MagicMock()
    client, _ = _client(habitify_sync=habitify_sync)
    with client:
        response = client.post(
            "/metrics",
            json={
                "key": "weight",
                "value": 91.2,
                "unit": "kg",
                "recorded_at": "2026-08-28T05:42:10+03:00",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json()["habitify_synced"] is True
    habitify_sync.complete.assert_called_once_with("Weigh in", "2026-08-28")


def test_habitify_failure_can_be_retried_without_duplicating_metric():
    from habitify import HabitifyError

    habitify_sync = MagicMock()
    habitify_sync.complete.side_effect = HabitifyError(503, "unavailable")
    client, logs = _client(habitify_sync=habitify_sync)
    with client:
        response = client.post(
            "/metrics",
            json={
                "key": "weight",
                "value": 91.2,
                "unit": "kg",
                "recorded_at": "2026-08-28T05:42:10+03:00",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 502
    assert response.json()["detail"]["metric_saved"] is True
    logs.write_metric.assert_called_once()


def test_real_metric_store_deduplicates_same_health_sample(tmp_path):
    logs = Logs(str(tmp_path))
    client, _ = _client(logs=logs)
    payload = {
        "key": "weight",
        "value": 91.2,
        "unit": "kg",
        "recorded_at": "2026-08-28T05:42:10+03:00",
    }
    headers = {"Authorization": f"Bearer {TOKEN}"}

    with client:
        first = client.post("/metrics", json=payload, headers=headers)
        second = client.post("/metrics", json=payload, headers=headers)

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    rows = logs.db.query("SELECT * FROM metrics WHERE key = 'weight'")
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-28"

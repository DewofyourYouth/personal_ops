"""Tests for location.py: the general travel-override singleton — resolve/apply
split (so a change is never applied without an explicit commit step upstream),
expiry, and listener notification (used by scheduling.py to move cron jobs
onto a new timezone when the active location changes)."""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import location

_MIAMI = (25.7617, -80.1918)


@pytest.fixture(autouse=True)
def _isolated_instance(tmp_path):
    location.init(str(tmp_path))
    yield


def _fake_nominatim(lat=None, lon=None, raises=False):
    def _factory(**kwargs):
        geocoder = MagicMock()
        if raises:
            geocoder.geocode.side_effect = RuntimeError("network down")
        elif lat is None:
            geocoder.geocode.return_value = None
        else:
            result = MagicMock()
            result.latitude, result.longitude = lat, lon
            geocoder.geocode.return_value = result
        return geocoder

    return _factory


class TestResolve:
    def test_resolve_has_no_side_effects(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        info = location.resolve("Miami")
        assert info is not None
        assert (info.latitude, info.longitude) == _MIAMI
        assert info.timezone == "America/New_York"
        # resolve() alone must not change the active location.
        assert location.current().name == location.DEFAULT_NAME

    def test_resolve_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(lat=None))
        assert location.resolve("Nowhereville") is None

    def test_resolve_network_error_returns_none(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(raises=True))
        assert location.resolve("Miami") is None


class TestApply:
    def test_apply_persists_and_shifts_tz(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        info = location.resolve("Miami")
        location.apply(info)
        assert location.current().name == "Miami"
        assert str(location.current_tz()) == "America/New_York"

    def test_apply_notifies_listeners(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        calls = []
        location.add_listener(lambda: calls.append(1))
        location.apply(location.resolve("Miami"))
        assert calls == [1]

    def test_set_travel_is_resolve_then_apply(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        info = location.set_travel("Miami")
        assert info is not None
        assert location.current().name == "Miami"

    def test_set_travel_not_found_does_not_apply(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(lat=None))
        assert location.set_travel("Nowhereville") is None
        assert location.current().name == location.DEFAULT_NAME


class TestClearTravel:
    def test_clear_reverts_to_default(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        location.set_travel("Miami")
        location.clear_travel()
        assert location.current().name == location.DEFAULT_NAME
        assert str(location.current_tz()) == location.DEFAULT_TZ

    def test_clear_with_nothing_set_is_a_no_op(self):
        location.clear_travel()  # must not raise
        assert location.current().name == location.DEFAULT_NAME

    def test_clear_only_notifies_if_something_changed(self):
        calls = []
        location.add_listener(lambda: calls.append(1))
        location.clear_travel()  # nothing was set — no notification
        assert calls == []


class TestExpiry:
    def test_expired_override_falls_back_to_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        location.set_travel("Miami")
        # Force the persisted expiry into the past.
        import json

        path = location._instance._override_path()
        data = json.loads(open(path).read())
        data["expires"] = (date.today() - timedelta(days=1)).isoformat()
        with open(path, "w") as f:
            json.dump(data, f)
        assert location.current().name == location.DEFAULT_NAME

    def test_unexpired_override_stays_active(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        location.set_travel("Miami")
        assert location.current().name == "Miami"

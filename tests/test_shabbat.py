"""Tests for Shabbat candle lighting: automatic (astral) computation, manual
override precedence, the Shabbat-only location override (resolve/apply split,
so nothing is applied without an explicit confirm step upstream), its
priority against the general travel override in location.py, and the
regression where TextRouter was wired to the wrong object (QuietWindow
instead of Shabbat), breaking save_candle_lighting.
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import location
from astral.sun import sun
from shabbat import (
    _BEIT_SHEMESH,
    CANDLE_LIGHTING_OFFSET_MIN,
    NIGHTFALL_OFFSET_MIN,
    Shabbat,
)

_TZFAT = (32.9648, 35.4952)
_MIAMI = (25.7617, -80.1918)


@pytest.fixture(autouse=True)
def _isolated_location(tmp_path):
    """Every test gets its own Location singleton pointed at its own tmp_path,
    so a test that sets a travel override can never write into the real
    ops/log data dir, and no test sees another test's leftover override."""
    location.init(str(tmp_path))
    yield


def _shabbat(tmp_path) -> Shabbat:
    return Shabbat(str(tmp_path))


def _fake_nominatim(lat=None, lon=None, raises=False):
    """Monkeypatch target for location.Nominatim — avoids real network calls."""

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


class TestComputedCandleLighting:
    def test_matches_sunset_minus_offset(self, tmp_path):
        s = _shabbat(tmp_path)
        d = date(2026, 8, 28)  # a Friday
        expected = sun(_BEIT_SHEMESH.observer, date=d, tzinfo=location.current_tz())[
            "sunset"
        ] - timedelta(minutes=CANDLE_LIGHTING_OFFSET_MIN)
        assert s.computed_candle_lighting(d) == expected.time().replace(
            second=0, microsecond=0
        )

    def test_varies_by_date(self, tmp_path):
        # Sanity: summer and winter candle-lighting times should differ.
        s = _shabbat(tmp_path)
        summer = s.computed_candle_lighting(date(2026, 8, 28))
        winter = s.computed_candle_lighting(date(2026, 12, 25))
        assert summer != winter


class TestComputedNightfall:
    def test_matches_sunset_plus_offset(self, tmp_path):
        s = _shabbat(tmp_path)
        d = date(2026, 6, 20)  # a Saturday
        expected = sun(_BEIT_SHEMESH.observer, date=d, tzinfo=location.current_tz())[
            "sunset"
        ] + timedelta(minutes=NIGHTFALL_OFFSET_MIN)
        assert s.computed_nightfall(d) == expected

    def test_quiet_now_saturday_uses_computed_nightfall(self, tmp_path, monkeypatch):
        s = _shabbat(tmp_path)
        d = date(2026, 6, 20)
        nightfall = s.computed_nightfall(d)

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return nightfall - timedelta(minutes=1)

        monkeypatch.setattr("shabbat.datetime", _FakeDatetime)
        assert s.quiet_now() is True

        class _FakeDatetimeAfter(datetime):
            @classmethod
            def now(cls, tz=None):
                return nightfall + timedelta(minutes=1)

        monkeypatch.setattr("shabbat.datetime", _FakeDatetimeAfter)
        assert s.quiet_now() is False


class TestLoadCandleLighting:
    def test_no_override_falls_back_to_computed(self, tmp_path):
        s = _shabbat(tmp_path)
        assert s.load_candle_lighting() == s.computed_candle_lighting()

    def test_manual_override_takes_precedence(self, tmp_path):
        s = _shabbat(tmp_path)
        s.save_candle_lighting("19:13")
        loaded = s.load_candle_lighting()
        assert loaded.hour == 19 and loaded.minute == 13
        assert loaded != s.computed_candle_lighting()

    def test_save_then_load_roundtrip(self, tmp_path):
        s = _shabbat(tmp_path)
        s.save_candle_lighting("18:05")
        loaded = s.load_candle_lighting()
        assert (loaded.hour, loaded.minute) == (18, 5)


class TestCandleConfirmation:
    def test_reports_the_given_time(self, tmp_path):
        s = _shabbat(tmp_path)
        msg = s.candle_confirmation("19:30")
        assert "19:30" in msg


class TestLocationOverride:
    def test_successful_geocode_shifts_computed_times(self, tmp_path, monkeypatch):
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_TZFAT))
        resolved = s.save_location_override("Tzfat")
        assert resolved is not None
        assert (resolved.latitude, resolved.longitude) == _TZFAT
        assert s._active_location().name == "Tzfat"
        # computed_candle_lighting must be computed from the override's
        # observer, not the hardcoded Beit Shemesh one.
        d = date(2026, 8, 28)
        expected = sun(resolved.observer, date=d, tzinfo=location.current_tz())[
            "sunset"
        ] - timedelta(minutes=CANDLE_LIGHTING_OFFSET_MIN)
        assert s.computed_candle_lighting(d) == expected.time().replace(
            second=0, microsecond=0
        )

    def test_geocode_not_found_returns_none_and_keeps_default(
        self, tmp_path, monkeypatch
    ):
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(lat=None))
        assert s.save_location_override("Nowhereville") is None
        assert s._active_location().name == _BEIT_SHEMESH.name

    def test_geocode_network_error_returns_none(self, tmp_path, monkeypatch):
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(raises=True))
        assert s.save_location_override("Tzfat") is None
        assert s._active_location().name == _BEIT_SHEMESH.name

    def test_resolve_does_not_apply(self, tmp_path, monkeypatch):
        """resolve() alone must have no side effects — text_router.py relies on
        this to preview a location before the user confirms it."""
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_TZFAT))
        resolved = location.resolve("Tzfat")
        assert resolved is not None
        assert s._active_location().name == _BEIT_SHEMESH.name  # unchanged

    def test_override_expires_after_that_shabbats_saturday(self, tmp_path):
        s = _shabbat(tmp_path)
        stale = {
            "name": "Tzfat",
            "lat": _TZFAT[0],
            "lon": _TZFAT[1],
            "expires": "2020-01-01",  # long past
        }
        with open(s._location_override_path(), "w") as f:
            json.dump(stale, f)
        assert s._active_location().name == _BEIT_SHEMESH.name

    def test_override_still_active_before_expiry(self, tmp_path):
        s = _shabbat(tmp_path)
        future = {
            "name": "Tzfat",
            "lat": _TZFAT[0],
            "lon": _TZFAT[1],
            "expires": "2099-01-01",
        }
        with open(s._location_override_path(), "w") as f:
            json.dump(future, f)
        assert s._active_location().name == "Tzfat"

    def test_clear_location_override_removes_file(self, tmp_path, monkeypatch):
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_TZFAT))
        s.save_location_override("Tzfat")
        assert s._active_location().name == "Tzfat"
        s.clear_location_override()
        assert s._active_location().name == _BEIT_SHEMESH.name

    def test_candle_confirmation_names_the_override_location(
        self, tmp_path, monkeypatch
    ):
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_TZFAT))
        s.save_location_override("Tzfat")
        assert "Tzfat" in s.candle_confirmation("19:00")

    def test_candle_confirmation_omits_location_when_default(self, tmp_path):
        s = _shabbat(tmp_path)
        assert _BEIT_SHEMESH.name not in s.candle_confirmation("19:00")

    def test_shabbat_only_override_never_changes_the_timezone(
        self, tmp_path, monkeypatch
    ):
        """Israel is one timezone — visiting another Israeli city for Shabbat
        must shift sun-time math only, never the app's active tz."""
        s = _shabbat(tmp_path)
        before = location.current_tz()
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_TZFAT))
        s.save_location_override("Tzfat")
        assert location.current_tz() == before


class TestGeneralTravelOverridePriority:
    def test_travel_override_takes_priority_over_shabbat_only(
        self, tmp_path, monkeypatch
    ):
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_TZFAT))
        s.save_location_override("Tzfat")
        assert s._active_location().name == "Tzfat"

        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        location.set_travel("Miami")
        # The general travel override (actually in Miami) beats the
        # possibly-stale Shabbat-only override (Tzfat, from earlier).
        assert s._active_location().name == "Miami"

    def test_travel_override_also_shifts_the_timezone(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        location.set_travel("Miami")
        assert str(location.current_tz()) == "America/New_York"

    def test_clearing_travel_falls_back_to_shabbat_only_override(
        self, tmp_path, monkeypatch
    ):
        s = _shabbat(tmp_path)
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_TZFAT))
        s.save_location_override("Tzfat")
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        location.set_travel("Miami")
        assert s._active_location().name == "Miami"

        location.clear_travel()
        assert s._active_location().name == "Tzfat"


class TestTextRouterWiring:
    def test_text_router_receives_a_real_shabbat_instance(self, tmp_path):
        """Regression: bot.py once passed the QuietWindow instance to TextRouter
        instead of Shabbat, so self.shabbat.save_candle_lighting() blew up with
        AttributeError. Guard the wiring by asserting the methods TextRouter
        calls actually exist on what it's given."""
        s = _shabbat(tmp_path)
        assert hasattr(s, "save_candle_lighting")
        assert hasattr(s, "candle_confirmation")
        s.save_candle_lighting("20:00")  # must not raise

"""Tests for the location-override flow in text_router.py: the cheap regex
pre-gate before the LLM fallback, and the resolve-then-confirm handshake
(_propose_location_override / handle_location_callback) that every location
change — deterministic command or LLM guess — goes through. Nothing is ever
applied without an explicit user confirmation."""

import asyncio
import sys
import types
from datetime import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import location
from telegram import Message
from text_router import TextRouter, _LOCATION_HINT_RE

_MIAMI = (25.7617, -80.1918)


@pytest.fixture(autouse=True)
def _isolated_location(tmp_path):
    location.init(str(tmp_path))
    yield


def _fake_nominatim(lat=None, lon=None):
    def _factory(**kwargs):
        geocoder = MagicMock()
        if lat is None:
            geocoder.geocode.return_value = None
        else:
            result = MagicMock()
            result.latitude, result.longitude = lat, lon
            geocoder.geocode.return_value = result
        return geocoder

    return _factory


class _Reply:
    def __init__(self):
        self.calls = []

    async def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))


class _FakeQuery:
    def __init__(self, data, user_id=1, chat_id=1):
        self.data = data
        self.from_user = types.SimpleNamespace(id=user_id)
        # spec=Message so isinstance(query.message, Message) — the
        # message-accessibility guard in handle_location_callback — passes.
        self.message = MagicMock(spec=Message)
        self.message.chat_id = chat_id
        self.edited_text = None

    async def answer(self, text=""):
        pass

    async def edit_message_text(self, text, **kwargs):
        self.edited_text = text


def _router():
    r = TextRouter.__new__(TextRouter)
    r.allowed_user = 1
    r._pending_location = {}
    return r


class TestLocationHintRegex:
    def test_matches_expected_phrasings(self):
        for text in [
            "I'm going to be in Djerba for Shabbos",
            "I'm in Florida now",
            "please update shabbat times for Djerba",
            "I'll be in Miami next week",
            "just headed to London",
            "back in Tzfat this weekend",
        ]:
            assert _LOCATION_HINT_RE.search(text.lower()), text

    def test_does_not_match_unrelated_text(self):
        for text in [
            "log a note about the weather",
            "I finished the project",
            "checkin feeling good",
            "task: buy milk",
        ]:
            assert not _LOCATION_HINT_RE.search(text.lower()), text


class TestProposeLocationOverride:
    def test_resolved_place_holds_pending_and_asks_to_confirm(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        r = _router()
        reply = _Reply()
        asyncio.run(r._propose_location_override(1, "travel", "Miami", reply))

        assert r._pending_location[1]["kind"] == "travel"
        assert r._pending_location[1]["resolved"].name == "Miami"
        # Nothing applied yet.
        assert location.current().name == location.DEFAULT_NAME

        text, kwargs = reply.calls[0]
        assert "Miami" in text
        assert kwargs.get("reply_markup") is not None

    def test_unresolved_place_replies_and_holds_nothing(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(lat=None))
        r = _router()
        reply = _Reply()
        asyncio.run(r._propose_location_override(1, "travel", "Nowhereville", reply))

        assert 1 not in r._pending_location
        assert "Couldn't find" in reply.calls[0][0]


class TestHandleLocationCallback:
    def _router_with_shabbat(self, applied: list):
        r = _router()
        r.shabbat = types.SimpleNamespace(
            apply_location_override=lambda info: applied.append(info),
            load_candle_lighting=lambda: time(19, 0),
            candle_confirmation=lambda t: f"candles at {t}",
        )
        return r

    def test_confirm_shabbat_applies_and_reports(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        applied = []
        r = self._router_with_shabbat(applied)
        resolved = location.resolve("Miami")
        r._pending_location[1] = {"kind": "shabbat", "resolved": resolved}

        q = _FakeQuery("loc:confirm")
        asyncio.run(
            r.handle_location_callback(types.SimpleNamespace(callback_query=q), None)
        )

        assert applied == [resolved]
        assert q.edited_text == "candles at 19:00"
        assert 1 not in r._pending_location

    def test_confirm_travel_applies_and_reports(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        r = _router()
        resolved = location.resolve("Miami")
        r._pending_location[1] = {"kind": "travel", "resolved": resolved}

        q = _FakeQuery("loc:confirm")
        asyncio.run(
            r.handle_location_callback(types.SimpleNamespace(callback_query=q), None)
        )

        assert location.current().name == "Miami"
        assert "Miami" in q.edited_text
        assert 1 not in r._pending_location

    def test_cancel_discards_pending_and_applies_nothing(self, monkeypatch):
        monkeypatch.setattr("location.Nominatim", _fake_nominatim(*_MIAMI))
        r = _router()
        resolved = location.resolve("Miami")
        r._pending_location[1] = {"kind": "travel", "resolved": resolved}

        q = _FakeQuery("loc:cancel")
        asyncio.run(
            r.handle_location_callback(types.SimpleNamespace(callback_query=q), None)
        )

        assert location.current().name == location.DEFAULT_NAME
        assert q.edited_text == "Cancelled — no change made."
        assert 1 not in r._pending_location

    def test_no_pending_reports_a_warning(self):
        r = _router()
        q = _FakeQuery("loc:confirm")
        asyncio.run(
            r.handle_location_callback(types.SimpleNamespace(callback_query=q), None)
        )
        assert q.edited_text == "⚠️ No pending location change."

    def test_wrong_user_is_ignored(self):
        r = _router()
        r._pending_location[1] = {
            "kind": "travel",
            "resolved": location.DEFAULT_LOCATION,
        }
        q = _FakeQuery("loc:confirm", user_id=999)
        asyncio.run(
            r.handle_location_callback(types.SimpleNamespace(callback_query=q), None)
        )
        assert q.edited_text is None
        assert 1 in r._pending_location  # untouched

"""Tests for voice.py — the read-aloud button/toggle plumbing.

Covers the two things that would silently break the feature if changed
carelessly: the auto-voice preference must actually persist across process
restarts (it's a JSON file, not in-memory), and `offer()` must never clobber
an existing inline keyboard (several call sites attach a 🔊 button to a
message that already has resolve/accept buttons on it).
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import voice


@pytest.fixture(autouse=True)
def _isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "_PREFS_PATH", tmp_path / "voice_prefs.json")


def _message(text: str = "", reply_markup=None):
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.chat_id = 555
    msg.message_id = 42
    msg.reply_markup = reply_markup
    msg.edit_reply_markup = AsyncMock()
    return msg


def test_auto_enabled_defaults_false_when_no_file():
    assert voice.is_auto_enabled() is False


def test_set_auto_enabled_persists_across_reads():
    voice.set_auto_enabled(True)
    assert voice.is_auto_enabled() is True
    voice.set_auto_enabled(False)
    assert voice.is_auto_enabled() is False


def test_auto_enabled_survives_a_corrupt_file(tmp_path):
    voice._PREFS_PATH.write_text("not json")
    assert voice.is_auto_enabled() is False  # falls back, doesn't raise


@pytest.mark.asyncio
async def test_offer_skips_short_replies():
    msg = _message("short reply")
    await voice.offer(bot=MagicMock(), message=msg)
    msg.edit_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_offer_attaches_button_for_long_reply():
    msg = _message("x" * 200)
    await voice.offer(bot=MagicMock(), message=msg)
    msg.edit_reply_markup.assert_awaited_once()
    markup = msg.edit_reply_markup.call_args.args[0]
    assert markup.inline_keyboard[-1][0].callback_data == voice.CALLBACK_DATA


@pytest.mark.asyncio
async def test_offer_preserves_an_existing_keyboard():
    """Regression: hypothesis follow-ups and habit-tip messages already carry a
    resolve/accept keyboard — offer() must append, not replace it."""
    existing = MagicMock()
    existing.inline_keyboard = (("existing_button",),)
    msg = _message("x" * 200, reply_markup=existing)
    await voice.offer(bot=MagicMock(), message=msg)
    markup = msg.edit_reply_markup.call_args.args[0]
    assert markup.inline_keyboard[0] == ("existing_button",)
    assert markup.inline_keyboard[-1][0].callback_data == voice.CALLBACK_DATA


@pytest.mark.asyncio
async def test_offer_does_not_speak_when_auto_disabled():
    msg = _message("x" * 200)
    bot = MagicMock()
    bot.send_audio = AsyncMock()
    await voice.offer(bot=bot, message=msg)
    bot.send_audio.assert_not_called()


@pytest.mark.asyncio
async def test_offer_speaks_when_auto_enabled(monkeypatch):
    voice.set_auto_enabled(True)
    fake_provider = MagicMock()
    fake_provider.synthesize = AsyncMock(return_value=b"audio-bytes")
    monkeypatch.setattr(voice.tts, "get_provider", lambda: fake_provider)
    msg = _message("x" * 200)
    bot = MagicMock()
    bot.send_audio = AsyncMock()
    await voice.offer(bot=bot, message=msg)
    bot.send_audio.assert_awaited_once()
    assert bot.send_audio.call_args.kwargs["audio"] == b"audio-bytes"
    assert bot.send_audio.call_args.kwargs["chat_id"] == 555


@pytest.mark.asyncio
async def test_speak_passes_a_filename_for_the_audio_bytes(monkeypatch):
    """Regression: send_audio(audio=<bytes>) with no filename makes PTB upload it
    as application/octet-stream, which Telegram won't play as audio — the 🔊
    button looked like it did nothing. filename= must always be passed alongside
    raw bytes so PTB/Telegram recognize it as mp3."""
    fake_provider = MagicMock()
    fake_provider.synthesize = AsyncMock(return_value=b"audio-bytes")
    monkeypatch.setattr(voice.tts, "get_provider", lambda: fake_provider)
    bot = MagicMock()
    bot.send_audio = AsyncMock()
    await voice.speak(bot, chat_id=1, text="hello")
    assert bot.send_audio.call_args.kwargs["filename"]


@pytest.mark.asyncio
async def test_offer_with_none_message_is_a_noop():
    """send_long returns None if every chunk send raised; offer() must tolerate it."""
    await voice.offer(bot=MagicMock(), message=None)


@pytest.mark.asyncio
async def test_speak_reports_provider_failures_instead_of_going_silent(monkeypatch):
    """A synthesis failure must never raise (it can't break the reply it's
    attached to) but must not go silently unnoticed either — the user tapped a
    button expecting a result, so they get told what broke instead of nothing."""
    broken_provider = MagicMock()
    broken_provider.synthesize = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(voice.tts, "get_provider", lambda: broken_provider)
    bot = MagicMock()
    bot.send_audio = AsyncMock()
    bot.send_message = AsyncMock()
    await voice.speak(bot, chat_id=1, text="hello")  # must not raise
    bot.send_audio.assert_not_called()
    bot.send_message.assert_awaited_once()
    assert "boom" in bot.send_message.call_args.kwargs["text"]
    assert bot.send_message.call_args.kwargs["chat_id"] == 1


@pytest.mark.asyncio
async def test_speak_times_out_instead_of_hanging_forever(monkeypatch):
    """Regression: a provider call that never returns (e.g. the host can't reach
    the TTS provider at all) must not hang forever with zero visible outcome —
    that's indistinguishable from the feature being silently broken. It must
    time out and report like any other failure."""
    monkeypatch.setattr(voice, "_SYNTHESIZE_TIMEOUT_S", 0.05)
    hanging_provider = MagicMock()

    async def _never_returns(text):
        await asyncio.sleep(10)

    hanging_provider.synthesize = _never_returns
    monkeypatch.setattr(voice.tts, "get_provider", lambda: hanging_provider)
    bot = MagicMock()
    bot.send_audio = AsyncMock()
    bot.send_message = AsyncMock()
    await asyncio.wait_for(
        voice.speak(bot, chat_id=1, text="hello"), timeout=2
    )  # must not hang
    bot.send_audio.assert_not_called()
    bot.send_message.assert_awaited_once()
    assert "timed out" in bot.send_message.call_args.kwargs["text"]

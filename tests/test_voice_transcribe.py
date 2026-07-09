"""Regression guard for the voice-intake event-loop stall.

transcribe_with_language_detection() is synchronous and makes a multi-second
Whisper network round-trip (up to two passes for Arabic/Hebrew). The whole bot
runs on one asyncio loop (PTB polling + the scheduler), so if the call runs
inline on the loop it freezes everything until it returns. handle_voice must
off-load it to a worker thread (asyncio.to_thread). We assert that by checking
the function executes on a different thread with no running event loop — the
inline-call bug would instead run it on the main thread with the loop running.
"""

import asyncio
import sys
import threading
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import text_router
from text_router import TextRouter


def _make_router():
    services = types.SimpleNamespace(
        logs=MagicMock(),
        agenda=MagicMock(),
        queue=MagicMock(),
        backlog=MagicMock(),
        reminders=MagicMock(),
        gcal=MagicMock(),
        planner=MagicMock(),
        hypotheses=MagicMock(),
    )
    return TextRouter(
        bot=AsyncMock(), services=services, shabbat=MagicMock(), allowed_user=123
    )


@pytest.mark.asyncio
async def test_voice_transcription_runs_off_event_loop(monkeypatch):
    router = _make_router()
    main_thread = threading.get_ident()
    calls = {}

    def fake_transcribe(path):
        calls["thread"] = threading.get_ident()
        try:
            asyncio.get_running_loop()
            calls["on_loop"] = True
        except RuntimeError:
            calls["on_loop"] = False
        return {
            "text": "hello world",
            "detected_language": "en",
            "was_second_pass": False,
        }

    monkeypatch.setattr(
        text_router, "transcribe_with_language_detection", fake_transcribe
    )
    monkeypatch.setattr(text_router, "send_sticker", AsyncMock())

    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 555
    update.message.voice.get_file = AsyncMock(return_value=AsyncMock())
    update.message.reply_text = AsyncMock()

    await router.handle_voice(update, context=MagicMock())

    assert calls["on_loop"] is False  # did NOT run on the event loop
    assert calls["thread"] != main_thread  # ran on a worker thread
    # The transcript still flows through to the confirm prompt + pending-edit state.
    assert router._awaiting_voice_edit[555] == "hello world"
    update.message.reply_text.assert_awaited()

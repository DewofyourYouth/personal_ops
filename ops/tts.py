"""tts.py — pluggable text-to-speech providers for reading bot output aloud.

A provider is anything satisfying `TTSProvider`: one async method that turns
text into audio bytes. Swapping providers (e.g. OpenAI → ElevenLabs, if
quality warrants it later) means adding one class here and changing
OPS_TTS_PROVIDER — voice.py and every call site stay provider-agnostic.

mp3 (not Telegram's voice-note OGG/Opus) is the deliberate output format: Bot
API's send_voice requires audio already muxed into an .ogg/OPUS container,
which providers don't hand back as-is, and transcoding would mean pulling in
ffmpeg for a personal tool. send_audio takes mp3 directly, so voice.py sends
audio through that instead — a regular playable attachment, not a voice-note
bubble.
"""

import os
from typing import Protocol, runtime_checkable

import openai

# OpenAI's TTS endpoint rejects input above this length; callers trim to fit.
MAX_INPUT_CHARS = 4096


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> bytes:
        """Return mp3 audio bytes for `text`."""
        ...


class OpenAITTSProvider:
    """OpenAI's TTS API (tts-1)."""

    def __init__(self, voice: str = "alloy") -> None:
        self.voice = voice

    async def synthesize(self, text: str) -> bytes:
        client = openai.AsyncOpenAI()
        response = await client.audio.speech.create(
            model="tts-1",
            voice=self.voice,
            input=text[:MAX_INPUT_CHARS],
            response_format="mp3",
        )
        return response.content


_PROVIDERS: dict[str, type] = {"openai": OpenAITTSProvider}


def get_provider(name: str | None = None) -> TTSProvider:
    """Build the configured provider. Reads OPS_TTS_PROVIDER (default 'openai')
    when `name` is not given."""
    name = (name or os.environ.get("OPS_TTS_PROVIDER", "openai")).lower()
    try:
        return _PROVIDERS[name]()
    except KeyError:
        raise ValueError(
            f"Unknown TTS provider {name!r} (available: {sorted(_PROVIDERS)})"
        )

"""Tests for the pluggable TTS provider factory in tts.py.

Provider selection must stay a one-line swap (OPS_TTS_PROVIDER + a new class) —
these lock in that the factory dispatches on the env var / explicit name and
fails loudly on an unknown provider, rather than silently falling back.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import tts


def test_default_provider_is_openai(monkeypatch):
    monkeypatch.delenv("OPS_TTS_PROVIDER", raising=False)
    assert isinstance(tts.get_provider(), tts.OpenAITTSProvider)


def test_explicit_name_overrides_env(monkeypatch):
    monkeypatch.setenv("OPS_TTS_PROVIDER", "openai")
    assert isinstance(tts.get_provider("openai"), tts.OpenAITTSProvider)


def test_env_var_selects_provider(monkeypatch):
    monkeypatch.setenv("OPS_TTS_PROVIDER", "OpenAI")  # case-insensitive
    assert isinstance(tts.get_provider(), tts.OpenAITTSProvider)


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown TTS provider"):
        tts.get_provider("elevenlabs")


def test_provider_satisfies_protocol():
    provider = tts.get_provider("openai")
    assert isinstance(provider, tts.TTSProvider)

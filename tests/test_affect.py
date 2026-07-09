"""Tests for the local affect-feature extraction (ops/affect.py).

Synthetic audio (numpy sine + silence, written with soundfile) keeps this fully
offline while exercising the real librosa pipeline — the parts worth locking in
are the pause detection, the feature schema the entry record stores, and the
guarantee that no cloud provider ever gets called.
"""

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

from affect import _SR, extract_affect, extract_affect_features

FEATURE_KEYS = {
    "pitch_var",
    "speech_rate",
    "pause_ms",
    "pause_count",
    "energy",
    "duration_s",
}


def _tone(seconds: float, freq: float = 150.0) -> np.ndarray:
    t = np.linspace(0, seconds, int(_SR * seconds), endpoint=False)
    return (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(_SR * seconds), dtype=np.float32)


def _write(tmp_path, y: np.ndarray) -> str:
    path = str(tmp_path / "note.wav")
    sf.write(path, y, _SR)
    return path


def test_gapped_speech_yields_full_feature_set(tmp_path):
    """A 'sentence, pause, sentence' note: every feature present, the interior
    pause detected, and speech rate derived from the transcript word count."""
    y = np.concatenate([_tone(0.6), _silence(0.5), _tone(0.6, freq=180)])
    feats = extract_affect_features(_write(tmp_path, y), word_count=6)

    assert set(feats) == FEATURE_KEYS
    assert feats["pause_count"] == 1
    assert 300 <= feats["pause_ms"] <= 700  # the 500ms gap, frame-quantised
    assert feats["energy"] > 0
    assert feats["duration_s"] == pytest.approx(1.7, abs=0.1)
    assert feats["speech_rate"] == pytest.approx(6 / 1.7, abs=0.5)
    assert feats["pitch_var"] >= 0  # two tones → finite, non-negative variance


def test_continuous_speech_has_no_pauses(tmp_path):
    feats = extract_affect_features(_write(tmp_path, _tone(1.0)), word_count=3)
    assert feats["pause_count"] == 0
    assert feats["pause_ms"] == 0


def test_leading_and_trailing_silence_are_not_pauses(tmp_path):
    """Dead air before/after speech is not prosody — only interior gaps count."""
    y = np.concatenate([_silence(0.6), _tone(0.8), _silence(0.6)])
    feats = extract_affect_features(_write(tmp_path, y), word_count=2)
    assert feats["pause_count"] == 0


def test_missing_word_count_leaves_speech_rate_none(tmp_path):
    feats = extract_affect_features(_write(tmp_path, _tone(0.5)))
    assert feats["speech_rate"] is None


def test_empty_audio_returns_none(tmp_path):
    assert extract_affect_features(_write(tmp_path, _silence(0.01))) is None


def test_unreadable_file_returns_none(tmp_path):
    bad = tmp_path / "not-audio.ogg"
    bad.write_bytes(b"definitely not audio")
    assert extract_affect_features(str(bad)) is None


def test_cloud_provider_is_a_stub_not_a_call(tmp_path, monkeypatch):
    """Zero third-party emotion APIs: anything but 'local' fails loudly at the
    extension point instead of silently reaching out."""
    monkeypatch.setenv("OPS_AFFECT_PROVIDER", "hume")
    with pytest.raises(NotImplementedError):
        extract_affect(_write(tmp_path, _tone(0.3)))


def test_affect_features_ride_on_the_entry_record(tmp_path):
    """The features are stored on the SAME event record as the transcript —
    the entries row's extra column and the JSONL line, not a separate event."""
    import json

    from logs import Logs

    logs = Logs(str(tmp_path))
    feats = {"pitch_var": 12.3, "speech_rate": 2.1, "pause_ms": 400, "energy": 0.05}
    entry_id = logs.write("checkin", "voice note", extra={"affect_features": feats})

    row = logs.db.entry_by_id(entry_id)
    assert json.loads(row["extra"])["affect_features"] == feats

    jsonl_files = list(Path(str(tmp_path)).glob("*.jsonl"))
    line = json.loads(jsonl_files[0].read_text().splitlines()[0])
    assert line["affect_features"] == feats
    assert line["content"] == "voice note"

    # recovery keeps the features attached (fresh DB, replay from JSONL)
    logs.db.execute("DELETE FROM entries")
    assert logs.sync_jsonl_to_db() == 1
    recovered = logs.db.query("SELECT * FROM entries")[0]
    assert json.loads(recovered["extra"])["affect_features"] == feats

"""Local prosodic affect features for voice notes — collection only, no analysis.

Extracts a small feature set from the audio with librosa (all local math):
pitch variance, speaking rate, pause length/count, and energy. The features
ride on the SAME entry record as the transcript (entries.extra + the JSONL
line), so the local proxy can later be checked against the user's own
self_mood_rating taps on the same message.

Deterministic core: no Telegram concerns, no network. The text router calls
extract_affect in a worker thread while it still has the downloaded .ogg.

EXTENSION POINT — cloud affect provider: deliberately NOT implemented. If the
local proxy proves insufficient, set OPS_AFFECT_PROVIDER to a provider name
and wire it in `_extract_affect_cloud` below. Until then, any non-"local"
value fails loudly rather than silently calling out to a third-party
emotion API.
"""

from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# Frame/hop sized for 16 kHz mono: 32 ms windows every 10 ms — fine enough to
# resolve inter-phrase pauses without inflating the frame count.
_SR = 16_000
_FRAME = 512
_HOP = 160
_HOP_MS = 1000 * _HOP / _SR

# A pause is ≥ 250 ms of near-silence *between* speech (leading/trailing
# silence is dead air, not prosody).
_MIN_PAUSE_MS = 250


def extract_affect(path: str, word_count: int | None = None) -> dict | None:
    """Provider dispatch. Local-only today; see the extension point note above."""
    provider = os.environ.get("OPS_AFFECT_PROVIDER", "local")
    if provider == "local":
        return extract_affect_features(path, word_count)
    return _extract_affect_cloud(path, provider)


def _extract_affect_cloud(path: str, provider: str) -> dict | None:
    """EXTENSION POINT — a cloud affect provider (e.g. Hume) would be wired in
    here. Intentionally unimplemented: local-only until the local proxy is
    shown insufficient against self_mood_rating ground truth."""
    raise NotImplementedError(
        f"Cloud affect provider {provider!r} is not wired in — "
        "OPS_AFFECT_PROVIDER supports only 'local'."
    )


def extract_affect_features(path: str, word_count: int | None = None) -> dict | None:
    """Prosodic features for one voice note. Returns None if the audio is
    empty/unreadable — the transcript is logged either way, so a failed
    feature pass must never block the entry."""
    import librosa  # heavy import; this function runs in a worker thread

    try:
        y, sr = librosa.load(path, sr=_SR, mono=True)
    except Exception:
        logger.exception("Affect: could not load audio %s", path)
        return None
    if y.size < _FRAME:
        return None
    duration_s = len(y) / sr

    rms = librosa.feature.rms(y=y, frame_length=_FRAME, hop_length=_HOP)[0]
    energy = float(rms.mean())

    pause_ms, pause_count = _pauses(rms)

    # Fundamental-frequency variance over voiced frames only — pyin's voicing
    # decision keeps silence/fricatives from polluting the variance.
    try:
        f0, voiced, _ = librosa.pyin(
            y, fmin=65, fmax=400, sr=sr, frame_length=2048, hop_length=_HOP
        )
        voiced_f0 = f0[voiced & np.isfinite(f0)] if f0 is not None else np.array([])
        pitch_var = float(np.var(voiced_f0)) if voiced_f0.size else 0.0
    except Exception:
        logger.exception("Affect: pitch tracking failed for %s", path)
        pitch_var = 0.0

    speech_rate = (
        round(word_count / duration_s, 2) if word_count and duration_s > 0 else None
    )
    return {
        "pitch_var": round(pitch_var, 1),
        "speech_rate": speech_rate,  # words/sec from the transcript
        "pause_ms": pause_ms,
        "pause_count": pause_count,
        "energy": round(energy, 5),
        "duration_s": round(duration_s, 1),
    }


def _pauses(rms: np.ndarray) -> tuple[int, int]:
    """Total pause milliseconds and pause count from the frame energies.

    Silence = below 10% of the note's 95th-percentile energy (relative, so a
    quiet recording isn't all 'pause'). Only interior runs count.
    """
    threshold = 0.1 * np.percentile(rms, 95)
    speech = np.flatnonzero(rms >= threshold)
    if speech.size == 0:
        return 0, 0
    min_frames = int(_MIN_PAUSE_MS / _HOP_MS)
    pause_ms, pause_count = 0, 0
    run = 0
    for silent in rms[speech[0] : speech[-1] + 1] < threshold:
        if silent:
            run += 1
            continue
        if run >= min_frames:
            pause_ms += int(run * _HOP_MS)
            pause_count += 1
        run = 0
    return pause_ms, pause_count

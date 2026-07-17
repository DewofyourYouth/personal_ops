"""Local KNN-over-embeddings entry classifier — a swappable alternative to the Haiku
classifier in ``llm.classify_entry``, kept side-by-side for comparison before any cutover.

Design:
- **Embeddings** via OpenAI ``text-embedding-3-small`` — already a project dependency
  (Whisper), so no new package. Vectors are cached to ``ops/log/embed_cache.json`` keyed
  by a hash of the text, so repeat runs and the live path never re-pay for the same
  string. The cache dir is gitignored.
- A new entry is classified by cosine-similarity **majority vote** (similarity-weighted)
  of its ``k`` nearest neighbours among a *curated* reference set of the user's own
  historically-tagged entries.
- ``classify_entry_embedding(text, db, extra_tags)`` mirrors ``llm.classify_entry``'s
  result (a single tag string) so the router can swap implementations behind the
  ``OPS_CLASSIFIER`` env flag without other changes.

This module owns no Telegram or scheduling concerns — it's deterministic-core: local
numpy math over cached vectors, with the one network call (embedding) isolated and cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import openai

from llm import _BASE_CLASSIFICATION_TAGS

_EMBED_MODEL = "text-embedding-3-small"
_CACHE_PATH = Path(__file__).parent / "log" / "embed_cache.json"

# The tags the classifier is allowed to emit — the inference targets, mirroring the LLM
# enum (minus the "log" fallback). Plugin tags (e.g. food) are added per-call.
_INFERENCE_TAGS = [tag for tag, _ in _BASE_CLASSIFICATION_TAGS if tag != "log"]

# Noise that would poison a KNN reference set: the recurring nudge prompt logged as a
# reminder, dismissed-reminder checkins, and garbled (mojibake) transcriptions.
_NUDGE_PREFIX = "What are you doing? Log it"
_MOJIBAKE_RE = re.compile(r"[À-ÿ][-ɏ]")


def _is_junk(content: str, min_len: int = 8) -> bool:
    c = content.strip()
    if len(c) < min_len:
        return True
    if c.lower() == "reminder dismissed" or c.startswith(_NUDGE_PREFIX):
        return True
    if _MOJIBAKE_RE.search(c) or "�" in c:
        return True
    return False


# --- Embedding cache ---------------------------------------------------------------


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        return json.loads(_CACHE_PATH.read_text())
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache))


def embed_texts(texts: list[str], cache: dict | None = None) -> np.ndarray:
    """Embed ``texts`` (order preserved), embedding only cache-misses via one batched
    API call per 256 items. Returns an (n, dim) float32 matrix."""
    cache = _load_cache() if cache is None else cache
    missing = [t for t in dict.fromkeys(texts) if _key(t) not in cache]
    if missing:
        client = openai.OpenAI()
        for i in range(0, len(missing), 256):
            chunk = missing[i : i + 256]
            resp = client.embeddings.create(model=_EMBED_MODEL, input=chunk)
            for t, item in zip(chunk, resp.data):
                cache[_key(t)] = item.embedding
        _save_cache(cache)
    return np.array([cache[_key(t)] for t in texts], dtype=np.float32)


def _normalize(vecs: np.ndarray) -> np.ndarray:
    return vecs / (np.linalg.norm(vecs, axis=-1, keepdims=True) + 1e-8)


# --- Reference set + classifier ----------------------------------------------------


def build_reference_set(
    db, tags: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """Curated (texts, labels) from the user's tagged entries — deduped, junk removed.

    Only the given ``tags`` (default: the inference-target enum) are included, so the
    reference set never contains the ``log`` junk-drawer, reminder spam, or mojibake that
    would corrupt majority vote.
    """
    tags = tags or _INFERENCE_TAGS
    texts: list[str] = []
    labels: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        for r in db.entries_by_tag(tag):
            c = r["content"].strip()
            if _is_junk(c) or c in seen:
                continue
            seen.add(c)
            texts.append(c)
            labels.append(tag)
    return texts, labels


class EmbeddingClassifier:
    """KNN classifier over a fixed reference set of (text, label) pairs."""

    def __init__(self, ref_texts: list[str], ref_labels: list[str], k: int = 5) -> None:
        self.k = k
        self.labels = list(ref_labels)
        self.vecs = _normalize(embed_texts(list(ref_texts)))

    def classify(self, text: str, k: int | None = None) -> str:
        return self.classify_with_confidence(text, k)[0]

    def classify_with_confidence(
        self, text: str, k: int | None = None
    ) -> tuple[str, float]:
        """(tag, confidence) — confidence is the winner's share of the similarity-
        weighted vote (1.0 = unanimous neighbours, ~1/n_labels = a coin toss).
        Drives the low-confidence reclassify prompt."""
        k = k or self.k
        q = _normalize(embed_texts([text]))[0]
        sims = self.vecs @ q
        top = np.argsort(-sims)[:k]
        votes: dict[str, float] = {}
        for i in top:
            votes[self.labels[i]] = votes.get(self.labels[i], 0.0) + float(sims[i])
        winner = max(votes, key=lambda t: votes[t])  # similarity-weighted majority
        total = sum(votes.values())
        return winner, (votes[winner] / total if total > 0 else 0.0)


_singleton: EmbeddingClassifier | None = None


def _classify_sync(text: str, db, extra_tags: list[dict] | None) -> tuple[str, float]:
    global _singleton
    if _singleton is None:
        tags = _INFERENCE_TAGS + [t["tag"] for t in (extra_tags or [])]
        texts, labels = build_reference_set(db, tags)
        _singleton = EmbeddingClassifier(texts, labels)
    return _singleton.classify_with_confidence(text)


def reset_singleton() -> None:
    """Drop the cached classifier so the next call rebuilds its reference set —
    called after reclassifications/retrains change the underlying labels."""
    global _singleton
    _singleton = None


def known_tags() -> set[str]:
    """Tags with at least one example in the current reference set.

    A tag with zero examples (e.g. a freshly-added plugin tag like "grocery" before
    any of its entries land in the ``entries`` table) can never win the KNN vote —
    but its absence doesn't lower the winning vote's confidence, so callers can't
    tell "confidently right" from "confidently wrong because the correct answer
    wasn't on the ballot" without this. Empty (not "unknown") before the singleton
    is built; call after a ``classify_*`` call so it reflects the classifier in use.
    """
    return set(_singleton.labels) if _singleton is not None else set()


async def classify_entry_embedding(
    text: str, db, extra_tags: list[dict] | None = None
) -> str:
    """Swap-in for ``llm.classify_entry``: classify by KNN over the user's own corpus.

    Lazily builds a single classifier from the curated reference set on first use.
    ``extra_tags`` (plugin tags, e.g. food) extend the label set so plugin-owned entries
    can be routed too. The blocking embed/vote runs in a thread so the bot's event loop
    stays free, matching how the LLM/transcription calls are offloaded elsewhere.
    """
    return (await classify_entry_embedding_confidence(text, db, extra_tags))[0]


async def classify_entry_embedding_confidence(
    text: str, db, extra_tags: list[dict] | None = None
) -> tuple[str, float]:
    """Like classify_entry_embedding, but also returns the vote-share confidence."""
    return await asyncio.to_thread(_classify_sync, text, db, extra_tags)

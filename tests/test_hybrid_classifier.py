"""Tests for the hybrid classification path (OPS_CLASSIFIER=hybrid, the default).

The hybrid runs the local embedding-KNN first and only pays for an LLM call when
the vote is weak. The branch logic is easy to get subtly wrong (which tag wins a
disagreement, what confidence survives), so each branch is locked in here with
both classifiers faked — no network.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import classifier as classifier_mod
import text_router as text_router_mod
from text_router import TextRouter


def _bare_router():
    """A TextRouter with only what _classify_hybrid touches."""
    r = TextRouter.__new__(TextRouter)
    r.reclassify = None  # threshold falls back to 0.55
    r.logs = types.SimpleNamespace(db=None)
    return r


def _fake_embed(tag, confidence):
    async def fake(text, db, extra_tags=None):
        return tag, confidence

    return fake


def _fake_llm(tag, calls=None):
    async def fake(text, extra_tags=None):
        if calls is not None:
            calls.append(text)
        return tag

    return fake


async def _raise(*args, **kwargs):
    raise RuntimeError("unavailable")


def test_confident_embedding_skips_the_llm(monkeypatch):
    calls = []
    monkeypatch.setattr(
        classifier_mod, "classify_entry_embedding_confidence", _fake_embed("win", 0.9)
    )
    monkeypatch.setattr(text_router_mod, "classify_entry", _fake_llm("note", calls))
    tag, conf = asyncio.run(_bare_router()._classify_hybrid("crushed it", []))
    assert (tag, conf) == ("win", 0.9)
    assert calls == []  # no LLM spend when the KNN vote is strong


def test_weak_vote_with_llm_agreement_is_upgraded_to_confident(monkeypatch):
    """Agreement means no low-confidence picker: confidence is lifted to threshold."""
    monkeypatch.setattr(
        classifier_mod, "classify_entry_embedding_confidence", _fake_embed("note", 0.3)
    )
    monkeypatch.setattr(text_router_mod, "classify_entry", _fake_llm("note"))
    tag, conf = asyncio.run(_bare_router()._classify_hybrid("some entry", []))
    assert tag == "note"
    assert conf >= 0.55


def test_weak_vote_with_disagreement_uses_llm_tag_and_keeps_low_confidence(monkeypatch):
    """Disagreement defers to the LLM's tag but the low confidence survives, so
    the reclassify picker still fires downstream."""
    monkeypatch.setattr(
        classifier_mod, "classify_entry_embedding_confidence", _fake_embed("note", 0.3)
    )
    monkeypatch.setattr(text_router_mod, "classify_entry", _fake_llm("task"))
    tag, conf = asyncio.run(_bare_router()._classify_hybrid("some entry", []))
    assert (tag, conf) == ("task", 0.3)


def test_embedding_failure_falls_back_to_llm_alone(monkeypatch):
    """No OpenAI key / empty reference set → the LLM answers with no confidence,
    matching the old default path."""
    monkeypatch.setattr(classifier_mod, "classify_entry_embedding_confidence", _raise)
    monkeypatch.setattr(text_router_mod, "classify_entry", _fake_llm("task"))
    tag, conf = asyncio.run(_bare_router()._classify_hybrid("some entry", []))
    assert (tag, conf) == ("task", None)


def test_llm_tiebreak_failure_keeps_the_knn_vote(monkeypatch):
    """A weak KNN vote is still better than 'log' — an LLM outage must not
    discard it."""
    monkeypatch.setattr(
        classifier_mod, "classify_entry_embedding_confidence", _fake_embed("note", 0.3)
    )
    monkeypatch.setattr(text_router_mod, "classify_entry", _raise)
    tag, conf = asyncio.run(_bare_router()._classify_hybrid("some entry", []))
    assert (tag, conf) == ("note", 0.3)


def test_confident_vote_with_untrained_plugin_tag_still_tiebreaks(monkeypatch):
    """A plugin tag with zero reference examples (e.g. "grocery" before any of its
    entries reach the DB) can never win the KNN vote — so a confident-looking vote
    for some other tag may just mean the right answer wasn't on the ballot.
    Regression test for "pick up lemons at the store" being misclassified #checkin
    at 0.62 confidence (above the 0.55 threshold) because no #grocery examples
    existed to compete with."""
    monkeypatch.setattr(
        classifier_mod, "classify_entry_embedding_confidence", _fake_embed("checkin", 0.9)
    )
    monkeypatch.setattr(classifier_mod, "known_tags", lambda: {"checkin", "task"})
    monkeypatch.setattr(text_router_mod, "classify_entry", _fake_llm("grocery"))
    extra_tags = [{"tag": "grocery", "description": "items to buy"}]
    tag, conf = asyncio.run(
        _bare_router()._classify_hybrid("pick up lemons at the store", extra_tags)
    )
    assert (tag, conf) == ("grocery", 0.9)


def test_confident_vote_with_trained_plugin_tag_skips_the_llm(monkeypatch):
    """Once a plugin tag has reference examples, a confident vote is trusted as
    normal — the cold-start tie-break doesn't fire forever."""
    calls = []
    monkeypatch.setattr(
        classifier_mod, "classify_entry_embedding_confidence", _fake_embed("grocery", 0.9)
    )
    monkeypatch.setattr(classifier_mod, "known_tags", lambda: {"checkin", "grocery"})
    monkeypatch.setattr(text_router_mod, "classify_entry", _fake_llm("grocery", calls))
    extra_tags = [{"tag": "grocery", "description": "items to buy"}]
    tag, conf = asyncio.run(
        _bare_router()._classify_hybrid("pick up more eggs", extra_tags)
    )
    assert (tag, conf) == ("grocery", 0.9)
    assert calls == []

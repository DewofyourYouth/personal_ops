"""Tests for the weekly active-learning retrain pass.

Embeddings are monkeypatched to deterministic keyword-cluster vectors so the
KNN math runs for real but nothing touches the network — the tests pin down
the bookkeeping that must not silently regress: which events a run consumes,
latest-correction-wins, the run marker advancing, and eval metrics existing.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

import classifier
import retrain
from logs import Logs
from reclassify_handlers import ReclassifyHandlers


def _fake_embed(texts, cache=None):
    """Two clean clusters: 'walk' texts near [1,0], 'meal' texts near [0,1].
    A tiny text-hash jitter keeps identical-cluster vectors distinct."""
    vecs = []
    for t in texts:
        base = [1.0, 0.0] if "walk" in t else [0.0, 1.0]
        jitter = (hash(t) % 97) / 10_000.0
        vecs.append([base[0] + jitter, base[1] + jitter])
    return np.array(vecs, dtype=np.float32)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(classifier, "embed_texts", _fake_embed)
    classifier.reset_singleton()
    return Logs(str(tmp_path))


def _seed_reference(logs):
    """A believable curated corpus: insights about walks, food entries about meals."""
    for i in range(6):
        logs.write("insight", f"noticed the walk clears my head, variant {i}")
        logs.write("food", f"meal with rice and chicken, variant {i}")


def test_no_events_is_a_noop(logs):
    _seed_reference(logs)
    summary = retrain.run_retrain(logs.db)
    assert summary["n_events"] == 0
    assert logs.db.last_retrain_event_id() == 0  # no run recorded


def test_run_consumes_events_and_records_metrics(logs):
    _seed_reference(logs)
    handlers = ReclassifyHandlers(None, logs, allowed_user=1)
    # A meal that the classifier had filed under insight, corrected by the user…
    wrong_id = logs.write("insight", "meal after shul was heavy, variant x")
    handlers.apply_reclassify(wrong_id, "food")
    # …and a validated-correct walk insight.
    right_id = logs.write("insight", "the walk before Shacharit set up the day")
    handlers.apply_confirm(right_id, "insight")

    summary = retrain.run_retrain(logs.db)

    assert summary["n_events"] == 2
    assert summary["n_reclassify"] == 1
    assert summary["n_confirm"] == 1
    assert summary["n_eval"] == 2
    # metrics are present and sane; with clean clusters the after-model is right
    assert 0.0 <= summary["accuracy_before"] <= 1.0
    assert summary["accuracy_after"] == 1.0
    assert 0.0 < summary["mean_confidence_after"] <= 1.0
    # the run marker advanced to the last consumed event
    events = logs.db.label_events_after(0)
    assert logs.db.last_retrain_event_id() == events[-1]["id"]


def test_second_run_only_sees_new_events(logs):
    _seed_reference(logs)
    handlers = ReclassifyHandlers(None, logs, allowed_user=1)
    entry = logs.write("insight", "meal variant a")
    handlers.apply_reclassify(entry, "food")
    assert retrain.run_retrain(logs.db)["n_events"] == 1

    # nothing new → noop, marker unchanged
    marker = logs.db.last_retrain_event_id()
    assert retrain.run_retrain(logs.db)["n_events"] == 0
    assert logs.db.last_retrain_event_id() == marker

    # one more correction → exactly one event in the next run
    entry2 = logs.write("food", "walk to the shuk, variant b")
    handlers.apply_reclassify(entry2, "insight")
    assert retrain.run_retrain(logs.db)["n_events"] == 1


def test_latest_correction_per_entry_wins(logs):
    _seed_reference(logs)
    handlers = ReclassifyHandlers(None, logs, allowed_user=1)
    entry = logs.write("insight", "meal variant fickle")
    handlers.apply_reclassify(entry, "checkin")  # first guess…
    handlers.apply_reclassify(entry, "food")  # …then the real fix

    examples, last_id = retrain.build_training_examples(logs.db, 0)
    assert len(examples) == 1
    assert examples[0]["label"] == "food"
    assert last_id == logs.db.label_events_after(0)[-1]["id"]


def test_uncurateable_labels_and_deleted_entries_are_skipped(logs):
    _seed_reference(logs)
    handlers = ReclassifyHandlers(None, logs, allowed_user=1)
    to_log = logs.write("insight", "meal variant junk-drawer")
    handlers.apply_reclassify(to_log, "log")  # KNN never emits log
    gone = logs.write("insight", "meal variant deleted later")
    handlers.apply_reclassify(gone, "food")
    logs.db.delete_entry(gone)

    examples, _ = retrain.build_training_examples(logs.db, 0)
    assert examples == []
    # the run still consumes the events so they aren't re-fed forever
    summary = retrain.run_retrain(logs.db)
    assert summary["n_events"] == 2
    assert logs.db.last_retrain_event_id() > 0


def test_format_summary_reports_delta():
    text = retrain.format_summary(
        {
            "n_events": 3,
            "n_reclassify": 2,
            "n_confirm": 1,
            "n_eval": 3,
            "accuracy_before": 1 / 3,
            "accuracy_after": 1.0,
            "mean_confidence_after": 0.8,
            "n_reference": 12,
        }
    )
    assert "33% → 100% ↑" in text
    assert "3 label events" in text

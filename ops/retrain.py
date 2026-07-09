"""Weekly active-learning pass for the local embedding-KNN classifier.

The KNN classifier (classifier.py) has no trained weights — "retraining" it
means updating its reference set, which is rebuilt from entries.tag. Because a
reclassify tap already corrects entries.tag at the moment it happens, the
corrected/confirmed texts flow into the reference set for free; what this job
adds is the active-learning bookkeeping:

- pull the reclassify/confirm label_events since the last run,
- measure before/after accuracy on exactly those human-labelled texts
  (before = reference set *without* them; after = with them, scored
  leave-one-out so a text never votes for itself),
- record the run + metrics in retrain_runs so regressions are visible,
- drop the cached classifier singleton so the live path rebuilds.

Everything is local numpy math over cached vectors, same as classifier.py —
no training APIs. (New texts may hit the same cached OpenAI *embedding*
endpoint the live classifier already uses; NOTE the spec asked for a local
sentence-transformer, but the codebase's local classifier is embedding-KNN,
so this follows the codebase.)

Deterministic core: no Telegram or scheduler concerns. bot.py wraps
run_retrain in the weekly job and sends format_summary to the user.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

import classifier

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")

# Labels the KNN can actually emit: the inference enum plus the rules/plugin
# tags corrections realistically land on. "log" is the junk drawer — including
# it as a reference tag would pull in every uncurated entry, so corrections
# *to* log are excluded from the eval set (the KNN never emits it anyway).
_EVAL_LABELS = set(classifier._INFERENCE_TAGS) | {"food", "habit"}

_K = 5  # match the live classifier's neighbour count


def build_training_examples(db, after_id: int) -> tuple[list[dict], int]:
    """Labelled examples from the reclassify/confirm events since `after_id`.

    Returns (examples, last_event_id). One example per entry — if an entry was
    corrected more than once, the latest event wins. Deleted entries and
    corrections to labels the KNN can't emit (see _EVAL_LABELS) are skipped.
    """
    events = db.label_events_after(after_id)
    if not events:
        return [], after_id
    last_id = events[-1]["id"]
    latest_per_entry: dict[int, dict] = {}
    for e in events:  # oldest first, so later events overwrite earlier ones
        latest_per_entry[e["ref_entry_id"]] = e
    examples = []
    for entry_id, e in latest_per_entry.items():
        if e["to_label"] not in _EVAL_LABELS:
            continue
        entry = db.entry_by_id(entry_id)
        if entry is None or classifier._is_junk(entry["content"]):
            continue
        examples.append(
            {
                "entry_id": entry_id,
                "text": entry["content"].strip(),
                "label": e["to_label"],
                "event_type": e["event_type"],
            }
        )
    return examples, last_id


def _knn_predict(
    ref_vecs: np.ndarray,
    ref_labels: list[str],
    query_vec: np.ndarray,
    exclude: set[int] = frozenset(),
    k: int = _K,
) -> tuple[str, float]:
    """Similarity-weighted majority vote, optionally masking reference indices
    (leave-one-out). Mirrors EmbeddingClassifier.classify_with_confidence."""
    sims = ref_vecs @ query_vec
    if exclude:
        sims = sims.copy()
        sims[list(exclude)] = -np.inf
    top = np.argsort(-sims)[:k]
    votes: dict[str, float] = {}
    for i in top:
        if not np.isfinite(sims[i]):
            continue
        votes[ref_labels[i]] = votes.get(ref_labels[i], 0.0) + float(sims[i])
    if not votes:
        return "", 0.0
    winner = max(votes, key=lambda t: votes[t])
    total = sum(votes.values())
    return winner, (votes[winner] / total if total > 0 else 0.0)


def evaluate(db, examples: list[dict]) -> dict:
    """Before/after accuracy on the new human-labelled texts.

    before — reference set with these texts *removed*: how the classifier
    would label them without the feedback (its live mistakes).
    after  — full reference set, scored leave-one-out so a text's own vector
    never votes for it. Also reports the after-model's mean confidence on
    these texts (shift in the confidence distribution).
    """
    tags = sorted(set(classifier._INFERENCE_TAGS) | {ex["label"] for ex in examples})
    ref_texts, ref_labels = classifier.build_reference_set(db, tags)
    if not ref_texts:
        return {}
    ref_vecs = classifier._normalize(classifier.embed_texts(ref_texts))
    new_texts = {ex["text"] for ex in examples}
    held_out = {i for i, t in enumerate(ref_texts) if t in new_texts}
    if len(held_out) >= len(ref_texts):
        return {}  # nothing left to vote with

    query_vecs = classifier._normalize(
        classifier.embed_texts([ex["text"] for ex in examples])
    )
    correct_before = correct_after = 0
    confidences_after = []
    for ex, q in zip(examples, query_vecs):
        pred_before, _ = _knn_predict(ref_vecs, ref_labels, q, exclude=held_out)
        own = {i for i in held_out if ref_texts[i] == ex["text"]}
        pred_after, conf = _knn_predict(ref_vecs, ref_labels, q, exclude=own)
        correct_before += pred_before == ex["label"]
        correct_after += pred_after == ex["label"]
        confidences_after.append(conf)
    n = len(examples)
    return {
        "n_eval": n,
        "accuracy_before": round(correct_before / n, 3),
        "accuracy_after": round(correct_after / n, 3),
        "mean_confidence_after": round(sum(confidences_after) / n, 3),
        "n_reference": len(ref_texts),
    }


def run_retrain(db) -> dict:
    """One active-learning pass. Returns the summary dict that was recorded."""
    after_id = db.last_retrain_event_id()
    examples, last_event_id = build_training_examples(db, after_id)
    events = db.label_events_after(after_id)
    summary: dict = {
        "n_events": len(events),
        "n_examples": len(examples),
        "n_reclassify": sum(1 for e in events if e["event_type"] == "reclassify"),
        "n_confirm": sum(1 for e in events if e["event_type"] == "confirm"),
    }
    if not events:
        logger.info("retrain: no new label events since id %d — nothing to do", after_id)
        return summary

    if examples:
        try:
            summary.update(evaluate(db, examples))
        except Exception:
            # Eval is diagnostics; a failure there must not lose the run marker
            # (or the same events would be re-consumed forever).
            logger.exception("retrain: eval failed; recording run without metrics")

    ts = datetime.now(TZ).isoformat(timespec="seconds")
    db.record_retrain_run(ts, last_event_id, len(events), json.dumps(summary))
    # The live classifier rebuilds its reference set (which now includes the
    # corrected/confirmed texts via entries.tag) on next use.
    classifier.reset_singleton()
    logger.info("retrain: %s", summary)
    return summary


def format_summary(summary: dict) -> str:
    """Short Telegram HTML report for the weekly job."""
    lines = [
        "🧠 <b>Classifier retrain</b>",
        f"{summary.get('n_events', 0)} label events "
        f"({summary.get('n_reclassify', 0)} corrections, "
        f"{summary.get('n_confirm', 0)} confirms)",
    ]
    if "accuracy_before" in summary:
        before, after = summary["accuracy_before"], summary["accuracy_after"]
        arrow = "↑" if after > before else ("↓" if after < before else "→")
        lines.append(
            f"Accuracy on corrected texts: {before:.0%} → {after:.0%} {arrow}"
        )
        lines.append(
            f"Mean confidence: {summary['mean_confidence_after']:.0%} "
            f"(reference set: {summary['n_reference']})"
        )
    elif summary.get("n_events"):
        lines.append("No evaluable examples (labels outside the KNN's range).")
    return "\n".join(lines)

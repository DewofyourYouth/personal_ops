"""Compare the embedding-KNN classifier against the Haiku LLM classifier on a held-out
sample of the user's own tagged entries — the accuracy report to consult before deciding
whether to cut the live path over.

Both classifiers are scored against the stored tag (ground truth) on the same held-out
split, over the label set both can actually emit (the LLM inference enum + food). The
embedding classifier's neighbours come only from the train split, so there's no leakage.

Run:
    venv/bin/python ops/eval_classifier.py [--k 5] [--test-frac 0.3] [--seed 0]
"""

from __future__ import annotations

import argparse
import asyncio
import random
from collections import Counter, defaultdict

from dotenv import load_dotenv

load_dotenv()

from classifier import EmbeddingClassifier, _is_junk  # noqa: E402
from db import Database  # noqa: E402
from llm import classify_entry  # noqa: E402

DB_PATH = "ops/log/ops.db"

# Label set both classifiers can emit: the LLM inference enum + the food plugin tag.
# (habit / injection / directive are rules-routed before the LLM, so scoring them here
# would unfairly penalise the LLM path, which never sees them.)
EVAL_TAGS = [
    "insight",
    "hypothesis",
    "note",
    "task",
    "friction",
    "win",
    "backlog",
    "checkin",
    "food",
]
FOOD_EXTRA = [{"tag": "food", "description": "a meal or food consumed"}]


def load_corpus(db) -> list[tuple[str, str]]:
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for tag in EVAL_TAGS:
        for r in db.entries_by_tag(tag):
            c = r["content"].strip()
            if _is_junk(c) or c in seen:
                continue
            seen.add(c)
            rows.append((c, tag))
    return rows


def split(rows, test_frac, seed):
    """Stratified per-tag train/test split so every tag is represented in both."""
    by_tag = defaultdict(list)
    for text, tag in rows:
        by_tag[tag].append(text)
    rng = random.Random(seed)
    train, test = [], []
    for tag, texts in by_tag.items():
        texts = texts[:]
        rng.shuffle(texts)
        n_test = max(1, round(len(texts) * test_frac)) if len(texts) > 1 else 0
        test += [(t, tag) for t in texts[:n_test]]
        train += [(t, tag) for t in texts[n_test:]]
    return train, test


async def llm_predict(texts: list[str]) -> list[str]:
    sem = asyncio.Semaphore(5)

    async def one(t):
        async with sem:
            try:
                return await classify_entry(t, extra_tags=FOOD_EXTRA)
            except Exception as e:  # noqa: BLE001
                return f"ERROR:{e}"

    return await asyncio.gather(*(one(t) for t in texts))


def report(name: str, y_true: list[str], y_pred: list[str]) -> None:
    correct = sum(t == p for t, p in zip(y_true, y_pred))
    print(
        f"\n=== {name} — overall accuracy: {correct}/{len(y_true)} "
        f"= {correct / len(y_true):.1%} ==="
    )
    per_tag_total = Counter(y_true)
    per_tag_correct = Counter(t for t, p in zip(y_true, y_pred) if t == p)
    print(f"  {'tag':<12}{'n':>4}{'correct':>9}{'acc':>7}")
    for tag in EVAL_TAGS:
        n = per_tag_total.get(tag, 0)
        if not n:
            continue
        c = per_tag_correct.get(tag, 0)
        print(f"  {tag:<12}{n:>4}{c:>9}{c / n:>7.0%}")
    # Most common confusions
    conf = Counter((t, p) for t, p in zip(y_true, y_pred) if t != p)
    if conf:
        print("  top confusions (true → pred):")
        for (t, p), n in conf.most_common(6):
            print(f"    {t} → {p}: {n}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--skip-llm",
        action="store_true",
        help="skip the (billable) Haiku pass — embedding-only, for k sweeps",
    )
    args = ap.parse_args()

    db = Database(DB_PATH)
    rows = load_corpus(db)
    print(f"Curated corpus: {len(rows)} entries across {len(EVAL_TAGS)} tags")
    print("  per tag:", dict(Counter(tag for _, tag in rows)))

    train, test = split(rows, args.test_frac, args.seed)
    print(f"Train: {len(train)}  Test (held-out): {len(test)}  k={args.k}")

    clf = EmbeddingClassifier([t for t, _ in train], [g for _, g in train], k=args.k)
    test_texts = [t for t, _ in test]
    y_true = [g for _, g in test]

    y_embed = [clf.classify(t) for t in test_texts]
    report("Embedding KNN", y_true, y_embed)

    if not args.skip_llm:
        y_llm = await llm_predict(test_texts)
        report("Haiku LLM", y_true, y_llm)


if __name__ == "__main__":
    asyncio.run(main())

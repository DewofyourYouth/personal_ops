"""One-off reviewed backfill: reclassify the historical `#log` junk-drawer and the retired
`#values` rows, and purge pure-noise entries (reminder-nudge prompts, "reminder dismissed").

Why this is careful:
- Readers use SQLite, but `sync_jsonl_to_db` replays the append-only JSONL and dedups by
  (ts, tag). Retagging the DB alone would make the next sync RE-INSERT the stale row as a
  duplicate. So every change is applied to the DB *and* the matching JSONL line together.
- The DB file is copied to ops/log/ops.db.bak-<ts> before any write.
- Dry-run by default: prints the full proposal and changes nothing. Re-run with --apply.

Proposals:
- Noise (nudge prompt / "reminder dismissed" / mojibake) → DELETE.
- `#values` → `#directive` when it reads as an instruction to the app; otherwise routed
  through the normal classifier (personal/emotional content → checkin/insight; anything
  opening "discreet/discrete" → discrete).
- `#log` → rules-first (`_classify_entry`) then the LLM classifier; rows that don't move
  are left as `#log`.

    venv/bin/python ops/backfill_tags.py            # dry-run
    venv/bin/python ops/backfill_tags.py --apply    # mutate DB + JSONL (after backup)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from classifier import _is_junk  # noqa: E402 (reuse the same junk predicate)
from db import Database  # noqa: E402
from llm import classify_entry  # noqa: E402
from text_router import TextRouter, _is_nutrition_breakdown  # noqa: E402

LOG_DIR = Path("ops/log")
DB_PATH = LOG_DIR / "ops.db"
FOOD_EXTRA = [{"tag": "food", "description": "a meal or food consumed"}]

# App-directive markers: an instruction to the system, not personal content.
_DIRECTIVE_MARKERS = (
    "the app",
    "the system",
    "the bot",
    "it should",
    "it must",
    "must not",
    "should not",
    "non-negotiable",
    "agenda laundering",
)
_FIRST_PERSON_FEELING = re.compile(
    r"\bi\s+(feel|am\s+feeling|felt|care|love|hate|want|wonder|think i'?m)\b", re.IGNORECASE
)


def looks_like_directive(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _DIRECTIVE_MARKERS) and not _FIRST_PERSON_FEELING.search(t)


async def propose_tag(content: str, source_tag: str) -> str:
    """Proposed action: 'DELETE', or a tag name. Returns source_tag to mean 'keep'."""
    if _is_junk(content):
        return "DELETE"
    if source_tag == "values" and looks_like_directive(content):
        return "directive"
    if re.match(r"^discre[et]", content.strip(), re.IGNORECASE):
        return "discrete"
    # Rules-first (deterministic) — catches structured food / nutrition.
    rule_tag, _ = TextRouter._classify_entry(content)
    if rule_tag != "log":
        return rule_tag
    if _is_nutrition_breakdown(content):
        return "food"
    # Fall through to the LLM classifier for the ambiguous middle.
    try:
        return await classify_entry(content, extra_tags=FOOD_EXTRA)
    except Exception:
        return source_tag  # keep on failure


def _update_jsonl(ts: str, old_tag: str, content: str, new_tag: str | None) -> bool:
    """Rewrite (or drop, if new_tag is None) the JSONL line matching this entry.
    Returns True if a line was changed. Matches on (ts, content) within the day's file."""
    fp = LOG_DIR / f"{ts[:10]}.jsonl"
    if not fp.exists():
        return False
    lines = fp.read_text().splitlines()
    out, changed = [], False
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            out.append(line)
            continue
        if not changed and obj.get("ts") == ts and obj.get("content") == content:
            changed = True
            if new_tag is None:
                continue  # drop the line
            obj["tag"] = new_tag
            out.append(json.dumps(obj, ensure_ascii=False))
        else:
            out.append(line)
    if changed:
        fp.write_text("\n".join(out) + "\n")
    return changed


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate DB + JSONL (default: dry-run)")
    ap.add_argument("--tags", default="log,values", help="comma-separated source tags to review")
    args = ap.parse_args()

    db = Database(str(DB_PATH))
    source_tags = [t.strip() for t in args.tags.split(",")]
    rows = [dict(r) for tag in source_tags for r in db.entries_by_tag(tag)]
    print(f"Reviewing {len(rows)} rows tagged {source_tags}…\n")

    changes = []  # (id, ts, old_tag, content, action)
    for r in rows:
        action = await propose_tag(r["content"], r["tag"])
        if action != r["tag"]:
            changes.append((r["id"], r["ts"], r["tag"], r["content"], action))

    summary = Counter(f"{old} → {act}" for _, _, old, _, act in changes)
    print("=== Proposed changes ===")
    for label, n in summary.most_common():
        print(f"  {n:4d}  {label}")
    print(f"  {'':4s}  ({len(rows) - len(changes)} rows unchanged)\n")

    print("=== Detail ===")
    for _id, ts, old, content, act in changes:
        verb = "DELETE" if act == "DELETE" else f"→ {act}"
        print(f"  [{old:>7}] {verb:<12} {content[:70]!r}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write these changes.")
        return

    backup = LOG_DIR / f"ops.db.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(DB_PATH, backup)
    print(f"\nBacked up DB → {backup}")

    applied = 0
    for _id, ts, old, content, act in changes:
        if act == "DELETE":
            db.delete_entry(_id)
            _update_jsonl(ts, old, content, None)
        else:
            db.update_entry_tag(_id, act)
            _update_jsonl(ts, old, content, act)
        applied += 1
    print(f"Applied {applied} changes to DB + JSONL.")


if __name__ == "__main__":
    asyncio.run(main())

"""Test for backfill_tags._update_jsonl — the JSONL half of a tag migration.

The recovery log is append-only and sync_jsonl_to_db dedups by (ts, tag), so a
DB-only retag would be resurrected as a duplicate on the next sync. This locks
in that a rename rewrites exactly the matching JSONL line and nothing else.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import backfill_tags


def test_update_jsonl_rewrites_only_the_matching_line(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_tags, "LOG_DIR", tmp_path)
    fp = tmp_path / "2026-07-01.jsonl"
    lines = [
        {"ts": "2026-07-01T10:00:00", "tag": "wrong", "content": "bus was late"},
        {"ts": "2026-07-01T11:00:00", "tag": "win", "content": "shipped it"},
    ]
    fp.write_text("\n".join(json.dumps(o) for o in lines) + "\n")

    changed = backfill_tags._update_jsonl(
        "2026-07-01T10:00:00", "wrong", "bus was late", "friction"
    )

    assert changed
    out = [json.loads(line) for line in fp.read_text().splitlines()]
    assert out[0]["tag"] == "friction"
    assert out[1] == lines[1]  # the other line is untouched


def test_update_jsonl_returns_false_when_no_line_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill_tags, "LOG_DIR", tmp_path)
    (tmp_path / "2026-07-01.jsonl").write_text(
        json.dumps({"ts": "2026-07-01T10:00:00", "tag": "win", "content": "x"}) + "\n"
    )
    assert not backfill_tags._update_jsonl(
        "2026-07-01T10:00:00", "wrong", "different content", "friction"
    )

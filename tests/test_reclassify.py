"""Tests for the reclassification feature — the correction loop is load-bearing:
label_events feeds the weekly retrain, entries.tag drives every reader in the
app, and the JSONL/DB pair must stay consistent or startup recovery resurrects
stale rows (the exact bug backfill_tags documents).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))

from logs import Logs
from reclassify_handlers import (
    PICKER_TAGS,
    ReclassifyHandlers,
    entry_actions_keyboard,
    picker_keyboard,
)


def _make(tmp_path):
    logs = Logs(str(tmp_path))
    handlers = ReclassifyHandlers(None, logs, allowed_user=1)
    return logs, handlers


def test_write_returns_entry_id(tmp_path):
    logs = Logs(str(tmp_path))
    entry_id = logs.write("note", "first")
    assert isinstance(entry_id, int)
    assert logs.db.entry_by_id(entry_id)["content"] == "first"
    # metrics land in the metrics table — no entries row id
    assert logs.write("metric", "steps 100", extra={"key": "steps", "value": 100}) is None


def test_reclassify_appends_event_and_updates_live_tag(tmp_path):
    logs, handlers = _make(tmp_path)
    entry_id = logs.write("log", "felt sharp after the morning walk")

    from_label = handlers.apply_reclassify(entry_id, "insight")

    assert from_label == "log"
    # live tag corrected — every reader keys off entries.tag
    assert logs.db.entry_by_id(entry_id)["tag"] == "insight"
    # append-only correction event with the original label preserved
    events = logs.db.label_events_after(0)
    assert len(events) == 1
    e = events[0]
    assert (e["event_type"], e["from_label"], e["to_label"], e["source"]) == (
        "reclassify",
        "log",
        "insight",
        "user_tap",
    )
    assert e["ref_entry_id"] == entry_id


def test_confirm_is_its_own_event_type(tmp_path):
    logs, handlers = _make(tmp_path)
    entry_id = logs.write("checkin", "feeling okay")

    handlers.apply_confirm(entry_id, "checkin")

    events = logs.db.label_events_after(0)
    assert len(events) == 1
    assert events[0]["event_type"] == "confirm"
    assert events[0]["from_label"] == events[0]["to_label"] == "checkin"
    # a confirm validates, it does not mutate
    assert logs.db.entry_by_id(entry_id)["tag"] == "checkin"


def test_reclassify_keeps_jsonl_and_db_consistent(tmp_path):
    """Regression guard: sync_jsonl_to_db dedups by (ts, tag). If the JSONL line
    kept the old tag after a reclassify, the next startup sync would re-insert
    the stale row as a duplicate entry."""
    logs, handlers = _make(tmp_path)
    entry_id = logs.write("log", "shipped the deploy")
    handlers.apply_reclassify(entry_id, "win")

    assert logs.sync_jsonl_to_db() == 0
    rows = logs.db.query("SELECT * FROM entries")
    assert len(rows) == 1
    assert rows[0]["tag"] == "win"


def test_label_event_jsonl_lines_never_replay_into_entries(tmp_path):
    logs, handlers = _make(tmp_path)
    entry_id = logs.write("log", "some text")
    handlers.apply_reclassify(entry_id, "note")
    handlers.apply_confirm(entry_id, "note")

    # the day's JSONL carries the label events for durability…
    jsonl = (Path(str(tmp_path)) / f"{logs.read_today()[0]['date']}.jsonl").read_text()
    tags = [json.loads(line)["tag"] for line in jsonl.splitlines()]
    assert tags.count("label_event") == 2
    # …but replay must never turn them into entries rows
    logs.sync_jsonl_to_db()
    assert len(logs.db.query("SELECT * FROM entries")) == 1


def test_content_edit_updates_db_and_jsonl(tmp_path):
    logs, _ = _make(tmp_path)
    entry_id = logs.write("note", "call the dentst")
    entry = logs.db.entry_by_id(entry_id)
    logs.db.update_entry_content(entry_id, "call the dentist")
    logs.rewrite_jsonl_entry(entry["ts"], entry["content"], new_content="call the dentist")

    assert logs.db.entry_by_id(entry_id)["content"] == "call the dentist"
    assert logs.sync_jsonl_to_db() == 0  # no stale resurrection


def test_fix_targets_latest_classified_entry(tmp_path):
    logs, _ = _make(tmp_path)
    logs.write("note", "older entry")
    newest = logs.write("insight", "newest classified entry")
    logs.write_metric("steps", 8000)  # metrics are not classified messages

    row = logs.db.latest_entry(exclude_tags=("metric", "reminder", "edit", "agenda"))
    assert row["id"] == newest
    assert row["content"] == "newest classified entry"


def test_callback_data_stays_under_telegram_64_byte_cap():
    """Entry ids are SQLite rowids; even absurdly large ids with the longest
    picker tag must fit Telegram's 64-byte callback_data limit."""
    huge_id = 10**12
    longest = max(PICKER_TAGS, key=len)
    for kbd in (
        entry_actions_keyboard(huge_id),
        picker_keyboard(huge_id, longest),
    ):
        for row in kbd.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64, btn.callback_data


def test_picker_premarks_current_tag_as_confirm():
    kbd = picker_keyboard(7, "checkin")
    flat = [btn for row in kbd.inline_keyboard for btn in row]
    marked = [b for b in flat if b.text.startswith("✅")]
    assert len(marked) == 1
    assert marked[0].text == "✅ checkin"
    assert marked[0].callback_data == "rc:keep:7:checkin"
    # all other categories are corrections
    others = [b for b in flat if b.callback_data.startswith("rc:set:")]
    assert len(others) == len(PICKER_TAGS) - 1


def test_reclassify_to_same_tag_returns_old_label(tmp_path):
    """A stale-keyboard tap on the same tag must not corrupt anything."""
    logs, handlers = _make(tmp_path)
    entry_id = logs.write("note", "text")
    assert handlers.apply_reclassify(entry_id, "note") == "note"
    assert logs.db.entry_by_id(entry_id)["tag"] == "note"


def test_reclassify_missing_entry_returns_none(tmp_path):
    _, handlers = _make(tmp_path)
    assert handlers.apply_reclassify(99999, "note") is None

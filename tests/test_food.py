import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from food_handlers import _macro_totals, _parse_macros
from logs import Logs
from text_router import _food_log_content, _format_food_estimate

_ESTIMATE = {
    "items": [
        {"name": "Lasagna", "portion": "~300g", "kcal": 420, "protein_g": 22},
        {"name": "Side salad", "portion": "~150g", "kcal": 60, "protein_g": 2},
    ],
    "total": {"kcal": 480, "protein_g": 24, "fat_g": 21, "carbs_g": 47},
}


def test_log_content_has_summary_and_items():
    """Food log content includes the raw meal summary and itemized estimate lines."""
    out = _food_log_content("lasagna and salad", _ESTIMATE)
    # Summary line carries the raw description and the totals.
    assert out.startswith("lasagna and salad — ~480 kcal, 24g protein")
    # Each item is itemised with its kcal.
    assert "• Lasagna (~300g): 420 kcal" in out
    assert "• Side salad (~150g): 60 kcal" in out


def test_preview_escapes_and_totals():
    """Food estimate previews escape user text and show total macro context."""
    preview = _format_food_estimate("lasagna & salad", _ESTIMATE)
    assert "lasagna &amp; salad" in preview  # HTML-escaped raw text
    assert "Total:" in preview and "~480 kcal" in preview
    assert "approximate" in preview


def test_parse_macros_round_trips_log_content():
    """Macro parsing can read the exact format written into food logs."""
    # Parse straight back out of what _food_log_content writes — guards the format
    # contract /foodlog relies on.
    content = _food_log_content("lasagna and salad", _ESTIMATE)
    assert _parse_macros(content) == {
        "kcal": 480.0,
        "protein_g": 24.0,
        "fat_g": 21.0,
        "carbs_g": 47.0,
    }


def test_parse_macros_none_without_estimate():
    """Entries without an estimate marker do not produce macro data."""
    assert _parse_macros("banana — forgot to estimate") is None


def test_macro_totals_sums_and_skips_unestimated():
    """Macro totals sum estimated entries while ignoring unestimated notes."""
    a = _food_log_content("breakfast", _ESTIMATE)  # 480/24/21/47
    b = _food_log_content("lunch", _ESTIMATE)  # 480/24/21/47
    totals = _macro_totals([a, b, "snack — no macros"])
    assert totals == {
        "kcal": 960.0,
        "protein_g": 48.0,
        "fat_g": 42.0,
        "carbs_g": 94.0,
    }


def test_macro_totals_none_when_nothing_estimated():
    """Macro totals return None when no entries contain parseable estimates."""
    assert _macro_totals(["banana", "an apple"]) is None


# --- Persistence tests ---

_TODAY = date(2024, 6, 10)
_TS = datetime(2024, 6, 10, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _make_logs() -> Logs:
    d = tempfile.mkdtemp()
    return Logs(d)


def test_save_food_summary_returns_totals():
    """save_food_summary computes and returns the day's macro totals."""
    logs = _make_logs()
    content = _food_log_content("lunch", _ESTIMATE)
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)

    totals = logs.save_food_summary(_TODAY)
    assert totals is not None
    assert totals["kcal"] == 480.0
    assert totals["protein_g"] == 24.0
    assert totals["fat_g"] == 21.0
    assert totals["carbs_g"] == 47.0


def test_save_food_summary_persists_to_db():
    """save_food_summary writes a row that food_summary_for_range can read back."""
    logs = _make_logs()
    content_a = _food_log_content("breakfast", _ESTIMATE)
    content_b = _food_log_content("lunch", _ESTIMATE)
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content_a)
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content_b)

    logs.save_food_summary(_TODAY)

    rows = logs.db.food_summary_for_range(_TODAY, _TODAY)
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == _TODAY.isoformat()
    assert r["kcal"] == 960.0
    assert r["protein_g"] == 48.0
    assert r["entry_count"] == 2


def test_save_food_summary_is_idempotent():
    """Calling save_food_summary twice for the same day upserts, not duplicates."""
    logs = _make_logs()
    content = _food_log_content("lunch", _ESTIMATE)
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)

    logs.save_food_summary(_TODAY)
    logs.save_food_summary(_TODAY)

    rows = logs.db.food_summary_for_range(_TODAY, _TODAY)
    assert len(rows) == 1


def test_save_food_summary_none_when_no_food():
    """save_food_summary returns None and writes nothing for a day with no food entries."""
    logs = _make_logs()
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "log", "some note")

    result = logs.save_food_summary(_TODAY)
    assert result is None
    rows = logs.db.food_summary_for_range(_TODAY, _TODAY)
    assert len(rows) == 0


def test_save_food_summary_skips_unestimated():
    """Entries without macro estimates don't block saving, but are excluded from totals."""
    logs = _make_logs()
    content = _food_log_content("lunch", _ESTIMATE)
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", "banana — no estimate")

    totals = logs.save_food_summary(_TODAY)
    assert totals is not None
    assert totals["kcal"] == 480.0  # only the estimated entry counted

    rows = logs.db.food_summary_for_range(_TODAY, _TODAY)
    assert rows[0]["entry_count"] == 2  # both entries counted regardless


def test_format_food_for_prompt_empty_when_no_data():
    """format_food_for_prompt returns empty string when nothing has been saved."""
    logs = _make_logs()
    assert logs.format_food_for_prompt(days=7) == ""


def test_format_food_for_prompt_includes_saved_day():
    """format_food_for_prompt shows a row for each day with saved summary data."""
    logs = _make_logs()
    content = _food_log_content("lunch", _ESTIMATE)
    logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)
    logs.save_food_summary(_TODAY)

    text = logs.format_food_for_prompt(days=7, end_date=_TODAY)
    assert _TODAY.isoformat() in text
    assert "480" in text
    assert "protein" in text

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from food_handlers import _macro_totals, _parse_macros
from text_router import _food_log_content, _format_food_estimate

_ESTIMATE = {
    "items": [
        {"name": "Lasagna", "portion": "~300g", "kcal": 420, "protein_g": 22},
        {"name": "Side salad", "portion": "~150g", "kcal": 60, "protein_g": 2},
    ],
    "total": {"kcal": 480, "protein_g": 24, "fat_g": 21, "carbs_g": 47},
}


def test_log_content_has_summary_and_items():
    out = _food_log_content("lasagna and salad", _ESTIMATE)
    # Summary line carries the raw description and the totals.
    assert out.startswith("lasagna and salad — ~480 kcal, 24g protein")
    # Each item is itemised with its kcal.
    assert "• Lasagna (~300g): 420 kcal" in out
    assert "• Side salad (~150g): 60 kcal" in out


def test_preview_escapes_and_totals():
    preview = _format_food_estimate("lasagna & salad", _ESTIMATE)
    assert "lasagna &amp; salad" in preview  # HTML-escaped raw text
    assert "Total:" in preview and "~480 kcal" in preview
    assert "approximate" in preview


def test_parse_macros_round_trips_log_content():
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
    assert _parse_macros("banana — forgot to estimate") is None


def test_macro_totals_sums_and_skips_unestimated():
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
    assert _macro_totals(["banana", "an apple"]) is None

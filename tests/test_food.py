import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
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

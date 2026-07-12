import sys
import tempfile
import types
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from food_handlers import _macro_totals, _parse_macros
from food_registry import FoodRegistry, parse_composition
from logs import Logs
from text_router import (
    TextRouter,
    _estimate_total,
    _food_log_content,
    _format_food_estimate,
    _parse_food_default_values,
    _registry_items,
)

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


# --- Personal food registry: lookup, composition, auto-promotion ---


def _make_registry() -> FoodRegistry:
    return FoodRegistry(_make_logs().db)


def test_parse_composition_single_item():
    assert parse_composition("protein shake") == [("protein shake", 1.0)]


def test_parse_composition_multiplier():
    """'protein shake x2' -> a single part with multiplier 2."""
    assert parse_composition("protein shake x2") == [("protein shake", 2.0)]


def test_parse_composition_plus_items():
    """'protein shake + banana' -> two separate parts, each multiplier 1."""
    assert parse_composition("protein shake + banana") == [
        ("protein shake", 1.0),
        ("banana", 1.0),
    ]


def test_registry_lookup_exact_after_set_default():
    reg = _make_registry()
    reg.set_default("Protein Shake", 130, 24, 0, 3)
    hit = reg.lookup("protein shake")
    assert hit is not None
    assert hit["exact"] is True
    assert hit["kcal"] == 130


def test_registry_lookup_no_hit_returns_none():
    reg = _make_registry()
    assert reg.lookup("protein shake") is None


def test_registry_lookup_fuzzy_substring_is_not_exact():
    """A longer utterance containing a known alias is a fuzzy, not exact, hit —
    callers must not skip confirmation on this (it might be a different meal)."""
    reg = _make_registry()
    reg.set_default("protein shake", 130, 24, 0, 3)
    hit = reg.lookup("protein shake with oats")
    assert hit is not None
    assert hit["exact"] is False


def test_registry_items_all_exact_enables_instant_log():
    """'protein shake' logs instantly with registry values — every part is an
    exact hit, so the caller can skip estimation and confirmation entirely."""
    reg = _make_registry()
    reg.set_default("protein shake", 130, 24, 0, 3)
    known_items, unmatched, all_exact = _registry_items(
        parse_composition("protein shake"), reg
    )
    assert all_exact is True
    assert not unmatched
    assert _estimate_total(known_items) == {
        "kcal": 130,
        "protein_g": 24.0,
        "fat_g": 0.0,
        "carbs_g": 3.0,
    }


def test_registry_items_multiplier_doubles_values():
    """'protein shake x2' doubles registry values correctly."""
    reg = _make_registry()
    reg.set_default("protein shake", 130, 24, 0, 3)
    known_items, unmatched, all_exact = _registry_items(
        parse_composition("protein shake x2"), reg
    )
    assert all_exact is True
    assert _estimate_total(known_items) == {
        "kcal": 260,
        "protein_g": 48.0,
        "fat_g": 0.0,
        "carbs_g": 6.0,
    }


def test_registry_items_partial_hit_leaves_remainder_for_the_llm():
    """'protein shake + banana' — the shake is known; the banana falls through to
    normal estimation (it has no registry entry of its own)."""
    reg = _make_registry()
    reg.set_default("protein shake", 130, 24, 0, 3)
    known_items, unmatched, all_exact = _registry_items(
        parse_composition("protein shake + banana"), reg
    )
    assert all_exact is False
    assert len(known_items) == 1
    assert unmatched == [("banana", 1.0)]


def test_record_correction_needs_two_matching_before_prompting():
    """Correcting a novel food's estimate twice with consistent values triggers
    a save-as-default prompt; a single correction never does."""
    reg = _make_registry()
    assert reg.record_correction("mystery bowl", 500, 30, 15, 40) is None
    proposal = reg.record_correction("mystery bowl", 510, 31, 14, 41)
    assert proposal is not None
    assert proposal["alias"] == "mystery bowl"
    assert proposal["kcal"] == 510


def test_record_correction_ignores_dissimilar_values():
    reg = _make_registry()
    reg.record_correction("mystery bowl", 500, 30, 15, 40)
    proposal = reg.record_correction("mystery bowl", 900, 60, 40, 90)
    assert proposal is None


def test_record_correction_none_once_registry_entry_exists():
    reg = _make_registry()
    reg.set_default("mystery bowl", 500, 30, 15, 40)
    proposal = reg.record_correction("mystery bowl", 505, 31, 15, 41)
    assert proposal is None


def test_suppress_prompt_blocks_reprompting_within_cooldown():
    """Declining suppresses re-prompting for the alias while the cooldown is active."""
    reg = _make_registry()
    reg.record_correction("mystery bowl", 500, 30, 15, 40)
    reg.record_correction("mystery bowl", 510, 31, 14, 41)
    reg.suppress_prompt("mystery bowl")
    proposal = reg.record_correction("mystery bowl", 505, 30, 15, 40)
    assert proposal is None


def test_suppress_prompt_expires_after_cooldown():
    """Backdating the suppression past ~30 days allows prompting again."""
    reg = _make_registry()
    reg.record_correction("mystery bowl", 500, 30, 15, 40)
    reg.record_correction("mystery bowl", 510, 31, 14, 41)
    reg.suppress_prompt("mystery bowl")
    reg.db.execute(
        "UPDATE food_registry_prompts SET last_ts = ? WHERE alias = ?",
        ("2000-01-01", "mystery bowl"),
    )
    proposal = reg.record_correction("mystery bowl", 512, 31, 15, 41)
    assert proposal is not None


def test_parse_food_default_values_all_present():
    """#default protein shake = 130kcal 24p 0f 3c — the explicit override parser."""
    assert _parse_food_default_values("130kcal 24p 0f 3c") == {
        "kcal": 130.0,
        "protein_g": 24.0,
        "fat_g": 0.0,
        "carbs_g": 3.0,
    }


def test_parse_food_default_values_incomplete_is_none():
    assert _parse_food_default_values("130kcal 24p") is None


# --- Food negations (append-only retraction) ---


def test_log_food_negation_leaves_original_entry_untouched():
    """'didn't finish the pizza' -> a negation is appended; the original entry
    is never deleted or mutated."""
    logs = _make_logs()
    content = _food_log_content("pizza", _ESTIMATE)
    entry_id = logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)

    negation_id = logs.log_food_negation(entry_id, 1.0, note="test")
    assert negation_id is not None
    assert logs.db.entry_by_id(entry_id)["content"] == content


def test_food_totals_for_entries_nets_full_negation():
    logs = _make_logs()
    content = _food_log_content("pizza", _ESTIMATE)  # 480/24/21/47
    entry_id = logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)
    logs.log_food_negation(entry_id, 1.0)

    totals = logs.food_totals_for_entries([{"id": entry_id, "content": content}])
    assert totals == {"kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}


def test_food_totals_for_entries_nets_partial_negation():
    """'only ate about a third' -> a 2/3 (~67%) negation, netted into the total."""
    logs = _make_logs()
    content = _food_log_content("pizza", _ESTIMATE)  # 480 kcal
    entry_id = logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)
    logs.log_food_negation(entry_id, 2 / 3)

    totals = logs.food_totals_for_entries([{"id": entry_id, "content": content}])
    assert round(totals["kcal"]) == round(480 - 480 * 2 / 3)


def test_log_food_negation_none_when_unparseable():
    logs = _make_logs()
    entry_id = logs.db.insert_entry(
        _TS, _TODAY.isoformat(), "food", "banana — no estimate"
    )
    assert logs.log_food_negation(entry_id, 1.0) is None


def test_save_food_summary_is_net_of_negations():
    """Daily/period totals are computed net of negations, not by mutating history."""
    logs = _make_logs()
    content = _food_log_content("pizza", _ESTIMATE)
    entry_id = logs.db.insert_entry(_TS, _TODAY.isoformat(), "food", content)
    logs.log_food_negation(entry_id, 1.0, note="undofood")

    totals = logs.save_food_summary(_TODAY)
    assert totals["kcal"] == 0.0


# --- Retraction target resolution (TextRouter) ---


def _make_router(logs: Logs) -> TextRouter:
    services = types.SimpleNamespace(
        logs=logs,
        agenda=MagicMock(),
        queue=MagicMock(),
        backlog=MagicMock(),
        reminders=MagicMock(),
        gcal=MagicMock(),
        planner=MagicMock(),
        hypotheses=MagicMock(),
        food_registry=FoodRegistry(logs.db),
    )
    return TextRouter(
        bot=AsyncMock(), services=services, shabbat=MagicMock(), allowed_user=123
    )


def test_find_food_entry_to_retract_bare_with_no_session_context_is_none():
    """'scratch that' with no prior logging this session -> no-op, doesn't guess."""
    logs = _make_logs()
    router = _make_router(logs)
    content = _food_log_content("pizza", _ESTIMATE)
    logs.db.insert_entry(_TS, date.today().isoformat(), "food", content)
    assert router._find_food_entry_to_retract(chat_id=1, item=None) is None


def test_find_food_entry_to_retract_bare_uses_session_pointer():
    logs = _make_logs()
    router = _make_router(logs)
    content = _food_log_content("pizza", _ESTIMATE)
    entry_id = logs.db.insert_entry(_TS, date.today().isoformat(), "food", content)
    router._last_food_entry[1] = entry_id
    found = router._find_food_entry_to_retract(chat_id=1, item=None)
    assert found is not None and found["id"] == entry_id


def test_find_food_entry_to_retract_named_match():
    """'didn't finish the pizza' (logged earlier same session) resolves by name,
    independent of session state — works across restarts."""
    logs = _make_logs()
    router = _make_router(logs)
    content = _food_log_content("pizza", _ESTIMATE)
    entry_id = logs.db.insert_entry(_TS, date.today().isoformat(), "food", content)
    found = router._find_food_entry_to_retract(chat_id=1, item="pizza")
    assert found is not None and found["id"] == entry_id


def test_find_food_entry_to_retract_no_match_returns_none():
    """No plausible match -> None, so the caller falls through instead of guessing."""
    logs = _make_logs()
    router = _make_router(logs)
    content = _food_log_content("pizza", _ESTIMATE)
    logs.db.insert_entry(_TS, date.today().isoformat(), "food", content)
    assert (
        router._find_food_entry_to_retract(chat_id=1, item="quarterly meeting notes")
        is None
    )

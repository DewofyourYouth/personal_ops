import sqlite3

from scripts.migrate_habits_to_habitify import (
    MigrationRule,
    habit_payload,
    normalize_name,
    python_days_to_habitify,
)


def test_python_weekdays_convert_to_habitify_weekdays():
    # Local Sun/Mon/Wed (6,0,2) becomes Habitify Sun/Mon/Wed (0,1,3).
    assert python_days_to_habitify("6,0,2") == [0, 1, 3]


def test_blank_schedule_means_every_non_shabbat_day():
    assert python_days_to_habitify("") == [0, 1, 2, 3, 4, 5]


def test_name_normalization_handles_case_punctuation_and_whitespace():
    assert normalize_name(" Brush teeth. ") == normalize_name("brush  teeth")


def test_create_payload_preserves_context_and_weekly_goal():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT 'Weekly anchors' section, 'x' name, '' days, "
        "'after coffee' cue, 'writer' identity"
    ).fetchone()
    rule = MigrationRule("Writing", ("Writing",), periodicity="weekly", goal_value=3)

    payload = habit_payload(row, rule, "2026-06-01")

    assert payload["occurrence"] == {
        "type": "weekDays",
        "days": [0, 1, 2, 3, 4, 5],
    }
    assert payload["goal"] == {"periodicity": "weekly", "value": 3, "unit": "rep"}
    assert "Cue: after coffee" in payload["description"]
    assert "Identity: writer" in payload["description"]

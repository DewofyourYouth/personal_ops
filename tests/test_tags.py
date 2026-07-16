"""Tests for the canonical tag registry (tags.py).

The taxonomy used to live in four files that could drift (bot_constants, llm,
reclassify_handlers, mine_logs). These tests lock in that everything derives
from one registry, and guard the wrong→friction rename.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from tags import (
    BASE_CLASSIFICATION_TAGS,
    PICKER_TAGS,
    PREFIXES,
    TAGS,
    TEXT_MINING_TAGS,
)
from text_router import TextRouter

classify = TextRouter._classify_entry


def test_every_prefix_maps_to_a_defined_tag():
    names = {t.name for t in TAGS}
    for prefix, hashtag in PREFIXES.items():
        assert hashtag.lstrip("#") in names, f"{prefix!r} maps to unknown {hashtag!r}"


def test_no_prefix_is_claimed_by_two_tags():
    all_prefixes = [p for t in TAGS for p in t.prefixes]
    assert len(all_prefixes) == len(set(all_prefixes))


def test_wrong_is_a_friction_alias():
    """The tag was renamed; the old prefix must keep working as muscle memory."""
    assert PREFIXES["friction:"] == "#friction"
    assert PREFIXES["wrong:"] == "#friction"
    assert classify("wrong: the bus was 25 minutes late") == (
        "friction",
        "the bus was 25 minutes late",
    )
    assert classify("friction: chavrusa cancelled again")[0] == "friction"


def test_classifier_enum_has_friction_not_wrong():
    enum = [tag for tag, _ in BASE_CLASSIFICATION_TAGS]
    assert "friction" in enum
    assert "wrong" not in enum


def test_picker_covers_every_inferable_tag():
    """Anything the classifier can emit must be correctable in the picker."""
    inferable = [tag for tag, _ in BASE_CLASSIFICATION_TAGS]
    for tag in inferable:
        assert tag in PICKER_TAGS


def test_mining_tags_are_defined_and_include_friction():
    names = {t.name for t in TAGS}
    assert set(TEXT_MINING_TAGS) <= names
    assert "friction" in TEXT_MINING_TAGS


def test_every_tag_has_a_definition():
    """The definition doubles as the classifier prompt line — it can't be empty."""
    for t in TAGS:
        assert t.definition.strip(), f"{t.name} has no definition"

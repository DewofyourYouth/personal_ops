import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context, CONTEXT_FILES


@pytest.fixture
def ctx(tmp_path):
    return Context(context_dir=tmp_path)


def test_read_missing_returns_empty(ctx):
    assert ctx.read("goals.md") == ""


def test_write_and_read(ctx):
    ctx.write("goals.md", "Ship Haki by Q3")
    assert ctx.read("goals.md") == "Ship Haki by Q3"


def test_write_strips_not_trim(ctx):
    ctx.write("priorities.md", "Focus on backend\n")
    assert ctx.read("priorities.md") == "Focus on backend"


def test_load_all_empty(ctx):
    assert ctx.load_all() == ""


def test_load_all_includes_written_files(ctx):
    ctx.write("goals.md", "Goal A")
    ctx.write("constraints.md", "Sleep 8h")
    result = ctx.load_all()
    assert "Goal A" in result
    assert "Sleep 8h" in result
    assert "### goals.md" in result
    assert "### constraints.md" in result


def test_load_all_skips_missing(ctx):
    ctx.write("goals.md", "Only this file")
    result = ctx.load_all()
    assert "priorities.md" not in result


def test_files_returns_expected_list(ctx):
    assert ctx.files() == CONTEXT_FILES


def test_overwrite(ctx):
    ctx.write("goals.md", "Version 1")
    ctx.write("goals.md", "Version 2")
    assert ctx.read("goals.md") == "Version 2"

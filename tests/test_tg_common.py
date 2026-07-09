"""Tests for shared Telegram UI helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from tg_common import mono_table


def test_mono_table_uses_pre_not_unsupported_table_tags():
    """Regression for /metrics and /foodlog silently failing.

    Both rendered a `<table>` with parse_mode=HTML, which Telegram rejects
    (`BadRequest: unsupported start tag "table"`) — real tables need the separate
    sendRichMessage API. The renderer must produce a supported <pre> block and never
    emit <table>/<tr>/<td>.
    """
    out = mono_table(["Time", "Mood"], [["morning", "7.2"], ["evening", "5"]])
    assert out.startswith("<pre>") and out.endswith("</pre>")
    for tag in ("<table", "<tr", "<td", "<th"):
        assert tag not in out
    # Cells survive and columns are padded to a common width.
    assert "morning" in out and "evening" in out and "7.2" in out


def test_mono_table_escapes_cell_contents():
    """Cell text is HTML-escaped so a stray '<' can't break the <pre> block."""
    out = mono_table(["Name"], [["a < b & c"]])
    assert "&lt; b &amp; c" in out
    assert "<pre>" in out  # the wrapper itself is not escaped

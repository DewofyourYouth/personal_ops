"""HTML→text extraction for the document-ingest handler. The LLM action extraction
isn't unit-tested (like the other LLM edges); the deterministic text scrape is."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from text_router import _html_to_text


def test_strips_tags_and_keeps_text():
    html = "<html><body><h1>Plan</h1><p>Call <b>Rev Galai</b> at 3pm.</p></body></html>"
    text = _html_to_text(html)
    assert "Plan" in text
    assert "Rev Galai" in text
    assert "<" not in text and ">" not in text


def test_skips_script_and_style():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>var secret = 1;</script><p>Visible</p></body></html>"
    )
    text = _html_to_text(html)
    assert "Visible" in text
    assert "secret" not in text
    assert "color:red" not in text


def test_empty_or_textless_html_yields_empty():
    assert _html_to_text("<html><body></body></html>").strip() == ""
    assert _html_to_text("") == ""

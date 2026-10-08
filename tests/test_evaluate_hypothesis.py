"""Tests for Planner.evaluate_hypothesis's data-awareness fix.

Bug: evaluate_hypothesis had zero visibility into what's already measurable — no
schema, no existing metric keys, nothing. Asked to set up a test for "voice
characteristics correlate with mood", it proposed hand-logging a brand new
voice_energy_flag metric every dictation, even though affect.py already extracts
prosody automatically and a self_mood_rating tap already exists specifically to
validate it against. These tests lock in that it now sees the existing metric
keys and the same entries.extra/metrics.key hint feedback()'s query planner uses,
so it can point at what's already there instead of proposing a redundant metric.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context
from logs import Logs
from planner import Planner

_HABITS_TABLE = """
CREATE TABLE habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT, name TEXT,
    days TEXT DEFAULT '', tracked INTEGER DEFAULT 1, position INTEGER DEFAULT 0,
    cue TEXT DEFAULT '', identity TEXT DEFAULT '',
    paused_from TEXT DEFAULT '', paused_until TEXT DEFAULT ''
)
"""


@pytest.fixture
def planner(tmp_path):
    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)  # empty — just needs to exist for the context block
    return Planner("claude-test", logs, context=Context(tmp_path))


def _fake_tool_response(**overrides):
    payload = {
        "restatement": "x",
        "confirm_if": "y",
        "falsify_if": "z",
        "metrics": [],
        "habits": [],
        "follow_up_days": 14,
        "follow_up_note": "check in",
        **overrides,
    }
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=payload)])


@pytest.mark.asyncio
async def test_evaluate_hypothesis_prompt_names_existing_metric_keys(planner):
    """The prompt lists whatever's already in metrics.key, so the model can
    reuse an existing key instead of inventing a duplicate."""
    planner.logs.write_metric("self_mood_rating", 4, "")
    planner.logs.write_metric("sleep", 7, "h")

    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(return_value=_fake_tool_response())
        mock_client.return_value.messages.create = create
        await planner.evaluate_hypothesis("voice speed correlates with my mood")

    system_text = create.call_args.kwargs["system"][0]["text"]
    assert "self_mood_rating" in system_text
    assert "sleep" in system_text


@pytest.mark.asyncio
async def test_evaluate_hypothesis_prompt_includes_hidden_data_note(planner):
    """Same entries.extra/metrics.key awareness feedback()'s query planner
    carries — this method had none of it before."""
    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(return_value=_fake_tool_response())
        mock_client.return_value.messages.create = create
        await planner.evaluate_hypothesis("voice speed correlates with my mood")

    system_text = create.call_args.kwargs["system"][0]["text"]
    assert "affect_features" in system_text
    assert "reuse the EXISTING key" in system_text


@pytest.mark.asyncio
async def test_evaluate_hypothesis_handles_no_metrics_logged_yet(planner):
    """A brand-new install with nothing in metrics yet must not crash the call,
    and still carries the general hidden-data note (it's true regardless of
    current data volume) even though there's no 'known taxonomy' listing to show."""
    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(return_value=_fake_tool_response())
        mock_client.return_value.messages.create = create
        result = await planner.evaluate_hypothesis("anything")

    assert result["restatement"] == "x"
    system_text = create.call_args.kwargs["system"][0]["text"]
    assert "affect_features" in system_text
    assert "Known taxonomy" not in system_text


@pytest.mark.asyncio
async def test_evaluate_hypothesis_surfaces_current_read_from_existing_data(planner):
    """Regression: 'voice speed correlates with mood' used to always propose
    hand-logging a brand new metric, even when affect_features + self_mood_rating
    already exist and already pair up. The query-planning step's results must
    reach the hypothesis-setup call so it can report what's already measurable."""
    planner.logs.write(
        "insight", "voice note", extra={"affect_features": {"speech_rate": 3.1}}
    )

    fake_plan_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                input={
                    "queries": [
                        "SELECT extra FROM entries WHERE extra LIKE '%affect_features%'"
                    ]
                },
            )
        ]
    )
    fake_setup_response = _fake_tool_response(
        current_read="12 voice notes have affect_features logged so far."
    )

    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(side_effect=[fake_plan_response, fake_setup_response])
        mock_client.return_value.messages.create = create
        result = await planner.evaluate_hypothesis(
            "voice speed correlates with my mood"
        )

    assert (
        result["current_read"] == "12 voice notes have affect_features logged so far."
    )
    setup_call_messages = create.call_args_list[-1].kwargs["messages"]
    user_content = setup_call_messages[0]["content"]
    assert "affect_features" in user_content
    assert "speech_rate" in user_content


@pytest.mark.asyncio
async def test_evaluate_hypothesis_current_read_defaults_empty(planner):
    """No queries planned (nothing relevant found) — current_read falls back to
    empty rather than the tool call failing to produce the field at all."""
    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(return_value=_fake_tool_response())
        mock_client.return_value.messages.create = create
        result = await planner.evaluate_hypothesis("brand new idea with no history")

    assert result["current_read"] == ""

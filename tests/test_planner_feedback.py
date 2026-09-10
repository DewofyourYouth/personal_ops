"""Tests for Planner.feedback()'s two-phase data retrieval.

feedback() used to always assemble the same fixed bundle of derived aggregates
(metric trends, food, daily stats) into the prompt, regardless of what was asked —
which meant it had no way to answer a question about, say, something logged three
days ago, or search for a specific past mention. It now asks the model what data
would answer THIS question (as SQL against the live schema), executes exactly that,
and only then generates the reflection. These tests cover the two building blocks
of that: the read-only SQL guard, and the query-execution/rendering step. The
query-planning LLM call itself isn't unit-tested (same as elsewhere in this file —
dedupe, split_task_items, etc.) but its result is exercised end-to-end here by
mocking the planning call's output and checking feedback() uses it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context
from logs import Logs
from planner import Planner, _is_safe_select

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


# --- _is_safe_select: the guard between LLM-generated SQL and the real DB ---


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM entries WHERE tag = 'insight'",
        "select content from entries where content like '%shacharit%'",
        "  SELECT ts, content FROM entries LIMIT 10  ",
        "SELECT * FROM entries;",  # single trailing semicolon is fine
    ],
)
def test_is_safe_select_accepts_plain_selects(sql):
    assert _is_safe_select(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE entries",
        "DELETE FROM entries WHERE 1=1",
        "SELECT * FROM entries; DROP TABLE entries",  # stacked statement
        "UPDATE entries SET content = 'x'",
        "PRAGMA table_info(entries)",
        "",
        "   ",
        "not sql at all",
    ],
)
def test_is_safe_select_rejects_writes_and_non_selects(sql):
    assert _is_safe_select(sql) is False


# --- feedback(): query planning result actually drives the prompt ---


@pytest.mark.asyncio
async def test_feedback_uses_planned_query_results(planner):
    """Rows from a planned query reach the final reflection prompt."""
    planner.logs.write(
        "insight", "Shacharit feels burdensome and I feel guilty skipping it"
    )

    fake_tool_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                input={
                    "queries": [
                        "SELECT content FROM entries WHERE content LIKE '%Shacharit%'"
                    ]
                },
            )
        ]
    )
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(side_effect=[fake_tool_response, fake_final_response])
        mock_client.return_value.messages.create = create
        await planner.feedback("react to what I just said about Shacharit")

    final_call_messages = create.call_args_list[-1].kwargs["messages"]
    user_content = final_call_messages[0]["content"]
    assert "Shacharit feels burdensome" in user_content


@pytest.mark.asyncio
async def test_feedback_skips_unsafe_planned_query(planner):
    """A planned query that fails the read-only guard never touches the DB or the prompt."""
    planner.logs.write("insight", "some private note")

    fake_tool_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                input={"queries": ["DROP TABLE entries"]},
            )
        ]
    )
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(side_effect=[fake_tool_response, fake_final_response])
        mock_client.return_value.messages.create = create
        await planner.feedback("anything?")

    # The table must still be intact (the unsafe query was never executed).
    assert planner.logs.db.query("SELECT COUNT(*) AS n FROM entries")[0]["n"] == 1


@pytest.mark.asyncio
async def test_feedback_falls_back_gracefully_when_planning_fails(planner):
    """A query-planning API error degrades to no per-question data, not a crash."""
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(side_effect=[RuntimeError("api down"), fake_final_response])
        mock_client.return_value.messages.create = create
        result = await planner.feedback("any thoughts?")

    assert result == "ok"

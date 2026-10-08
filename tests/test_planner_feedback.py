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


@pytest.mark.asyncio
async def test_feedback_query_planning_prompt_surfaces_hidden_data(planner):
    """Regression: the planner once confidently told the user that voice-note
    prosody (affect_features) and the self_mood_rating ground-truth tap weren't
    tracked anywhere — both are real, just stored somewhere a plain read of the
    column names wouldn't reveal (a JSON blob in entries.extra; a metrics.key
    value outside the obvious mood/energy/sleep/steps/weight set). The planning
    prompt must name both explicitly and push the model to run a discovery
    query instead of guessing from column names and concluding 'not tracked'."""
    fake_tool_response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={"queries": [], "reports": []})]
    )
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(side_effect=[fake_tool_response, fake_final_response])
        mock_client.return_value.messages.create = create
        await planner.feedback("does my voice speed correlate with my mood?")

    planning_call_system = create.call_args_list[0].kwargs["system"]
    assert "affect_features" in planning_call_system
    assert "self_mood_rating" in planning_call_system
    assert "DISTINCT" in planning_call_system


# --- feedback(): pre-built reports for things SQL alone can't compute ---


@pytest.mark.asyncio
async def test_feedback_query_planning_prompt_advertises_the_affect_report(planner):
    """Regression: once the planner knew the data existed, it still tried to
    answer a correlation question from a raw row dump instead of running the
    actual correlation mine_logs.py already computes. The planning prompt must
    offer that report by name and discourage eyeballing a correlation itself."""
    fake_tool_response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={"queries": [], "reports": []})]
    )
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(side_effect=[fake_tool_response, fake_final_response])
        mock_client.return_value.messages.create = create
        await planner.feedback("does my voice speed correlate with my mood?")

    planning_call_kwargs = create.call_args_list[0].kwargs
    reports_schema = planning_call_kwargs["tools"][0]["input_schema"]["properties"][
        "reports"
    ]
    assert "voice_affect_correlation" in reports_schema["items"]["enum"]
    assert "voice_affect_correlation" in reports_schema["description"]
    assert "eyeball" in planning_call_kwargs["system"]


@pytest.mark.asyncio
async def test_feedback_requested_report_reaches_the_final_prompt(planner):
    """A report the planner requests actually gets run and its output reaches
    the reflection prompt, not just raw joined rows the model would have to
    estimate a correlation from itself."""
    fake_tool_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                input={"queries": [], "reports": ["voice_affect_correlation"]},
            )
        ]
    )
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with (
        patch("planner.anthropic.AsyncAnthropic") as mock_client,
        patch(
            "mine_logs.affect_report_for",
            return_value="═══ AFFECT PROXY REPORT ═══\n   speech_rate    r=+0.41  (n=12)",
        ) as fake_report,
    ):
        create = AsyncMock(side_effect=[fake_tool_response, fake_final_response])
        mock_client.return_value.messages.create = create
        await planner.feedback("does my voice speed correlate with my mood?")

    fake_report.assert_called_once_with(planner.logs.db.path)
    final_call_messages = create.call_args_list[-1].kwargs["messages"]
    user_content = final_call_messages[0]["content"]
    assert "r=+0.41" in user_content


@pytest.mark.asyncio
async def test_feedback_report_failure_degrades_gracefully(planner):
    """No paired data yet (or any other report failure) must not crash feedback —
    it just means less context for this reply."""
    fake_tool_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                input={"queries": [], "reports": ["voice_affect_correlation"]},
            )
        ]
    )
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with (
        patch("planner.anthropic.AsyncAnthropic") as mock_client,
        patch("mine_logs.affect_report_for", side_effect=RuntimeError("db hiccup")),
    ):
        create = AsyncMock(side_effect=[fake_tool_response, fake_final_response])
        mock_client.return_value.messages.create = create
        result = await planner.feedback("does my voice speed correlate with my mood?")

    assert result == "ok"


@pytest.mark.asyncio
async def test_feedback_ignores_an_unknown_report_name(planner):
    """A hallucinated report name outside _FEEDBACK_REPORTS is dropped rather
    than attempted — the enum constrains the tool schema, but a belt-and-braces
    filter on the way out matters too since model output isn't guaranteed to
    respect the schema."""
    fake_tool_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                input={"queries": [], "reports": ["not_a_real_report"]},
            )
        ]
    )
    fake_final_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    with (
        patch("planner.anthropic.AsyncAnthropic") as mock_client,
        patch("mine_logs.affect_report_for") as fake_report,
    ):
        create = AsyncMock(side_effect=[fake_tool_response, fake_final_response])
        mock_client.return_value.messages.create = create
        await planner.feedback("anything?")

    fake_report.assert_not_called()

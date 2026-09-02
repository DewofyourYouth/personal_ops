"""Regression test: Planner.feedback() must include today's actual logged entries.

Bug: feedback() only ever assembled derived aggregates (metric trends, food, daily
stats) into the prompt — never the raw text of what the user logged. Asking it to
"react to what I just said" had nothing to react to: the LLM literally never saw the
content of that log entry, only rollup numbers.
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


@pytest.mark.asyncio
async def test_feedback_includes_todays_raw_log_content(planner):
    """A same-day log entry's actual text reaches the model, not just aggregates."""
    planner.logs.write("insight", "Shacharit feels burdensome and I feel guilty skipping it")

    fake_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(return_value=fake_response)
        mock_client.return_value.messages.create = create
        await planner.feedback("react to what I just said about Shacharit")

    sent_messages = create.call_args.kwargs["messages"]
    user_content = sent_messages[0]["content"]
    assert "Shacharit feels burdensome" in user_content


@pytest.mark.asyncio
async def test_feedback_omits_log_section_when_nothing_logged_today(planner):
    """No same-day entries: the prompt doesn't claim there's a log section with nothing in it."""
    fake_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        create = AsyncMock(return_value=fake_response)
        mock_client.return_value.messages.create = create
        await planner.feedback("any thoughts?")

    sent_messages = create.call_args.kwargs["messages"]
    user_content = sent_messages[0]["content"]
    assert "Today's log entries" not in user_content

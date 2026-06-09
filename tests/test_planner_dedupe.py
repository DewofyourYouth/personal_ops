"""Regression tests for Planner.dedupe — specifically its safety fallback.

The dedup step is a convenience layered onto the agenda proposal; it must never be
able to *lose* proposed items if the LLM call misbehaves. These tests lock in that
invariant without making real network calls (the dedup judgment itself is an LLM
call and, like propose, is not unit-tested).
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from planner import Planner


@pytest.fixture
def planner(tmp_path):
    # dedupe only touches the anthropic client; a stub logs with the attributes the
    # constructor reads is enough to build a Planner.
    logs = SimpleNamespace(log_dir=str(tmp_path), db=None)
    return Planner("claude-test", logs)


@pytest.mark.asyncio
async def test_dedupe_empty_proposed_returns_empty(planner):
    """An empty proposal short-circuits without asking the LLM to dedupe."""
    # No proposed items: short-circuit, no client call at all.
    assert await planner.dedupe(["already open"], []) == []


@pytest.mark.asyncio
async def test_dedupe_falls_back_to_proposed_on_api_error(planner):
    """Planner.dedupe preserves proposed items when the LLM API raises."""
    proposed = ["Call Rev Galai", "Write drasha"]
    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        mock_client.return_value.messages.create = AsyncMock(
            side_effect=RuntimeError("api down")
        )
        result = await planner.dedupe([], proposed)
    # A dedup hiccup must never drop items — the proposal goes through unchanged.
    assert result == proposed


@pytest.mark.asyncio
async def test_dedupe_falls_back_when_model_returns_nothing(planner):
    """Planner.dedupe preserves proposed items when the LLM response is unusable."""
    proposed = ["Call Rev Galai"]
    fake_response = SimpleNamespace(content=[SimpleNamespace(text="(no items)")])
    with patch("planner.anthropic.AsyncAnthropic") as mock_client:
        mock_client.return_value.messages.create = AsyncMock(return_value=fake_response)
        result = await planner.dedupe([], proposed)
    # An unparseable / empty reply is treated as "no opinion", not "drop everything".
    assert result == proposed

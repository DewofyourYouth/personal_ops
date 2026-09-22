"""Tests for "For tomorrow's agenda, ..." — an agenda destination stated with the
agenda phrase FIRST and the item(s) after, the mirror image of the existing "add X
to my agenda" shape (_AGENDA_DEST_RE). A voice note phrased this way used to fall
through to the classifier, get tagged a single generic #task, and never reach any
agenda at all — this is the regression it fixes.
"""

import sys
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from text_router import TextRouter, _AGENDA_FOR_DAY_RE, _resolve_agenda_for_day

_TOMORROW = date.today() + timedelta(days=1)


# --- _AGENDA_FOR_DAY_RE / _resolve_agenda_for_day: pure matching + day resolution ---


@pytest.mark.parametrize(
    "text,day_word,rest",
    [
        (
            "For tomorrow's agenda, I need to prepare for the interview, do some "
            "of the Coursera work.",
            "tomorrow's",
            "I need to prepare for the interview, do some of the Coursera work.",
        ),
        ("for today's agenda, water the plants", "today's", "water the plants"),
        ("for my agenda, call the dentist", "my", "call the dentist"),
        ("For the agenda: file taxes", "the", "file taxes"),
        ("For Sunday's agenda: renew passport", "Sunday's", "renew passport"),
    ],
)
def test_agenda_for_day_matches_and_extracts(text, day_word, rest):
    m = _AGENDA_FOR_DAY_RE.match(text)
    assert m is not None, text
    assert m.group(1) == day_word
    assert m.group(2) == rest


@pytest.mark.parametrize(
    "text",
    [
        "the meeting agenda was long",  # doesn't start with 'for'
        "add milk to the shopping list",
        "I'm looking forward to tomorrow's agenda item",  # 'for' isn't the leading word
    ],
)
def test_agenda_for_day_does_not_fire_without_the_phrase(text):
    assert _AGENDA_FOR_DAY_RE.match(text) is None


def test_resolve_agenda_for_day_today_synonyms():
    for word in ("my", "the", "today", "today's"):
        assert _resolve_agenda_for_day(word) == date.today()


def test_resolve_agenda_for_day_tomorrow():
    assert _resolve_agenda_for_day("tomorrow's") == _TOMORROW
    assert _resolve_agenda_for_day("tomorrow") == _TOMORROW


def test_resolve_agenda_for_day_weekday_name():
    resolved = _resolve_agenda_for_day("Sunday's")
    assert resolved is not None
    assert resolved.weekday() == 6  # Sunday
    assert resolved >= date.today()


# --- End-to-end dispatch through process_text ---


def _make_router(planner=None):
    services = types.SimpleNamespace(
        logs=MagicMock(),
        agenda=MagicMock(),
        queue=MagicMock(),
        backlog=MagicMock(),
        reminders=MagicMock(),
        gcal=MagicMock(),
        weekly_goals=MagicMock(),
        planner=planner or MagicMock(),
        hypotheses=MagicMock(),
        food_registry=MagicMock(),
    )
    router = TextRouter(
        bot=AsyncMock(), services=services, shabbat=MagicMock(), allowed_user=123
    )
    router.agenda_feature = MagicMock()
    return router


@pytest.mark.asyncio
async def test_for_tomorrows_agenda_queues_each_split_item():
    """The exact reported bug: a multi-item voice note phrased 'For tomorrow's
    agenda, ...' must split into distinct items and queue each for tomorrow —
    not land as one lumped #task that never reaches any agenda."""

    class _FakePlanner:
        async def split_task_items(self, text):
            return [
                "prepare for the interview",
                "do some of the Coursera work",
                "get Alulov and Etrog",
                "start with Asuka",
            ]

    router = _make_router(planner=_FakePlanner())
    reply = AsyncMock()

    await router.process_text(
        "For tomorrow's agenda, I need to prepare for the interview, do some of "
        "the Coursera work. I need to get Alulov and Etrog, and I should "
        "probably start with Asuka.",
        reply,
    )

    assert router.queue.add.call_args_list == [
        call("prepare for the interview", _TOMORROW),
        call("do some of the Coursera work", _TOMORROW),
        call("get Alulov and Etrog", _TOMORROW),
        call("start with Asuka", _TOMORROW),
    ]
    router.agenda_feature.commit_agenda.assert_not_called()
    reply.assert_called_once()
    assert "Queued for" in reply.call_args.args[0]


@pytest.mark.asyncio
async def test_for_todays_agenda_commits_directly_not_queued():
    """'today'/'my'/'the' agenda goes straight to today's real agenda, same as
    the existing 'add X to my agenda' phrasing — not the future-day queue."""

    class _FakePlanner:
        async def split_task_items(self, text):
            raise AssertionError("single item — split must not be called")

    router = _make_router(planner=_FakePlanner())
    reply = AsyncMock()

    await router.process_text("For today's agenda, call the dentist", reply)

    router.queue.add.assert_not_called()
    router.agenda_feature.commit_agenda.assert_called_once_with(
        ["call the dentist"], source="user"
    )
    reply.assert_called_once()
    assert "today's agenda" in reply.call_args.args[0]


@pytest.mark.asyncio
async def test_for_day_agenda_single_item_skips_split_call():
    """No 'and'/comma in the extracted item — the LLM split is never invoked."""

    class _FakePlanner:
        async def split_task_items(self, text):
            raise AssertionError("should not be called for a single-item note")

    router = _make_router(planner=_FakePlanner())
    reply = AsyncMock()

    await router.process_text("For tomorrow's agenda, renew my passport", reply)

    router.queue.add.assert_called_once_with("renew my passport", _TOMORROW)

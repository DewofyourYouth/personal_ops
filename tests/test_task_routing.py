"""Tests for the #task/#backlog routing buttons (route:<dest>:<entry_id>).

A classified #task or #backlog used to be a label and nothing more — the entry
never reached the agenda or the Backlog service unless retyped with an explicit
prefix. These tests lock in the one-tap routing: the right destination gets the
entry's content, and the routing row locks while other rows survive.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from telegram import InlineKeyboardButton

from text_router import TextRouter
from tg_common import inline_keyboard_markup, inline_keyboard_rows


class _FakeQuery:
    def __init__(self, data, markup, user_id=1):
        self.data = data
        self.from_user = types.SimpleNamespace(id=user_id)
        self.message = types.SimpleNamespace(reply_markup=markup)
        self.edited_markup = "unset"

    async def answer(self, text=""):
        pass

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edited_markup = reply_markup


def _router(entry):
    r = TextRouter.__new__(TextRouter)
    r.allowed_user = 1
    r.logs = types.SimpleNamespace(
        db=types.SimpleNamespace(entry_by_id=lambda _id: entry)
    )
    r.backlog = types.SimpleNamespace(
        added=[], add=lambda text: r.backlog.added.append(text)
    )
    r.agenda_feature = types.SimpleNamespace(
        committed=[],
        commit_agenda=lambda texts, source: r.agenda_feature.committed.append(
            (texts, source)
        ),
    )
    return r


def _markup_with_routing_row(entry_id, tag):
    rc_row = [InlineKeyboardButton("✏️ Edit", callback_data=f"rc:edit:{entry_id}")]
    return inline_keyboard_markup([TextRouter._routing_row(entry_id, tag), rc_row])


def test_routing_row_shapes():
    task_row = TextRouter._routing_row(5, "task")
    assert [b.callback_data for b in task_row] == ["route:agenda:5", "route:backlog:5"]
    backlog_row = TextRouter._routing_row(7, "backlog")
    assert [b.callback_data for b in backlog_row] == ["route:backlog:7"]
    assert TextRouter._routing_row(9, "checkin") is None
    assert TextRouter._routing_row(9, "log") is None


def test_route_to_backlog_adds_content_and_locks_row():
    entry = {"content": "learn some Rust someday"}
    r = _router(entry)
    q = _FakeQuery("route:backlog:5", _markup_with_routing_row(5, "backlog"))
    asyncio.run(r.handle_route_callback(types.SimpleNamespace(callback_query=q), None))

    assert r.backlog.added == ["learn some Rust someday"]
    assert r.agenda_feature.committed == []
    rows = inline_keyboard_rows(q.edited_markup)
    # routing row replaced by a locked confirmation; the rc row survives
    assert rows[0][0].callback_data == "noop"
    assert rows[1][0].callback_data == "rc:edit:5"


def test_route_to_agenda_commits_as_user_item():
    entry = {"content": "call the dentist"}
    r = _router(entry)
    q = _FakeQuery("route:agenda:5", _markup_with_routing_row(5, "task"))
    asyncio.run(r.handle_route_callback(types.SimpleNamespace(callback_query=q), None))

    assert r.agenda_feature.committed == [(["call the dentist"], "user")]
    assert r.backlog.added == []


def test_route_ignores_other_users():
    entry = {"content": "call the dentist"}
    r = _router(entry)
    q = _FakeQuery("route:agenda:5", _markup_with_routing_row(5, "task"), user_id=999)
    asyncio.run(r.handle_route_callback(types.SimpleNamespace(callback_query=q), None))
    assert r.agenda_feature.committed == []
    assert q.edited_markup == "unset"


def test_route_for_deleted_entry_clears_keyboard():
    r = _router(None)
    q = _FakeQuery("route:backlog:5", _markup_with_routing_row(5, "backlog"))
    asyncio.run(r.handle_route_callback(types.SimpleNamespace(callback_query=q), None))
    assert r.backlog.added == []
    assert q.edited_markup is None

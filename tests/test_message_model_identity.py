"""A content-only rebuild must not reset the message model.

A reset drops the view's contentY to zero, and the restore can only run once
layout has happened — so the reset was visible: pressing send flashed the top
of the conversation for a frame before snapping back to the bottom.

Binding a guid to a sent message is exactly that case: `_upgrade_echo_guid`
changes one field on a message that is already on screen.
"""
from __future__ import annotations

import pytest

from iphonebridge.qtui.models import MessageListModel, ThreadStore


@pytest.fixture
def store():
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {"k": {"key": "k", "messages": [], "name": "e",
                        "is_group": False}}
    s._by_phone = {}
    s._seen_handles = set()
    s._deleted = {}
    s._by_guid = {}
    s._unsorted = set()
    s._current = "k"
    s._pending_receipts = {}
    s._highlight_event_id = 0
    s._highlight_guid = ""
    s._message_model = MessageListModel()
    s._contacts = type("C", (), {"resolve_photo": lambda self, p: None})()
    for i in range(3):
        s._threads["k"]["messages"].append(
            {"body": f"m{i}", "ts": f"2026-07-30T12:0{i}:00+00:00",
             "outgoing": True, "reaction": None, "attachments": [],
             "guid": "", "reply_to": "", "sender_name": "",
             "sender_phone": "", "state": ""})
    s._rebuild_messages()
    return s


def _counts(model):
    resets, changes = [], []
    model.modelAboutToBeReset.connect(lambda: resets.append(1))
    model.dataChanged.connect(lambda *a: changes.append(1))
    return resets, changes


def test_binding_a_guid_updates_in_place(store):
    """What a send does once the daemon echoes it back."""
    resets, changes = _counts(store._message_model)
    store._bind_guid(store._threads["k"]["messages"][-1], "NEWGUID")
    store._rebuild_messages()
    assert resets == []
    assert changes  # repainted, just not reset
    assert store._message_model.rowCount() == 3


def test_edited_text_updates_in_place(store):
    resets, _ = _counts(store._message_model)
    store._threads["k"]["messages"][0]["body"] = "corrected"
    store._rebuild_messages()
    assert resets == []
    assert store._message_model.rows()[0]["body"] == "corrected"


def test_a_changed_shape_still_resets(store):
    """The in-place path is only safe when the rows line up; a different
    number of them has to reset or the view shows stale content."""
    resets, _ = _counts(store._message_model)
    store._threads["k"]["messages"].append(
        {"body": "new", "ts": "2026-07-30T12:05:00+00:00", "outgoing": False,
         "reaction": None, "attachments": [], "guid": "", "reply_to": "",
         "sender_name": "", "sender_phone": "", "state": ""})
    store._rebuild_messages()
    assert len(resets) == 1


def test_row_keys_are_unique_and_stable(store):
    """The identity is per (message, row within message). Duplicates would
    make two different bubbles compare equal and silently stop updating."""
    keys = [r["rowKey"] for r in store._message_model.rows()]
    assert len(keys) == len(set(keys))
    before = list(keys)
    store._rebuild_messages()
    assert [r["rowKey"] for r in store._message_model.rows()] == before


def test_multi_row_message_gets_distinct_keys(store):
    """A photo and its caption are two rows of one message."""
    msg = store._threads["k"]["messages"][0]
    msg["attachments"] = [{"path": "/tmp/x.png", "mime": "image/png",
                           "name": "x.png"}]
    rows = store._rows_for(msg, None)
    assert len(rows) == 2
    assert rows[0]["rowKey"] != rows[1]["rowKey"]

"""Clearing the sidebar search must restore the conversation list.

A finishing FTS used to re-apply hits after the field was emptied (model
reset re-enters the event loop; trailing dispatch treated pending!="" as
“run again”), which left searchActive stuck true with results permanently
replacing the thread list.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QObject, QTimer

from verdigris.qtui.models import MessageListModel, SearchResultModel, ThreadStore


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def store(qapp):
    s = ThreadStore.__new__(ThreadStore)
    QObject.__init__(s)
    s._threads = {
        "tel:1": {
            "key": "tel:1", "messages": [], "name": "A",
            "is_group": False, "phone": "1", "unread": 0,
            "last_ts": "", "last_preview": "", "messages_loaded": True,
            "has_older": False,
        },
    }
    s._by_phone = {}
    s._seen_handles = set()
    s._deleted = {}
    s._by_guid = {}
    s._unsorted = set()
    s._current = "tel:1"
    s._pending_receipts = {}
    s._highlight_event_id = 0
    s._highlight_guid = ""
    s._pinned = []
    s._search_query = ""
    s._search_pending = ""
    s._search_gen = 0
    s._search_inflight = False
    s._search_again = False
    s._search_model = SearchResultModel()
    s._message_model = MessageListModel()
    s._thread_model = type("T", (), {"rowCount": lambda self: 1})()
    s._contacts = type(
        "C",
        (),
        {
            "resolve": lambda self, p: None,
            "resolve_photo": lambda self, p: None,
        },
    )()
    s._search_timer = QTimer()
    s._search_timer.setSingleShot(True)
    return s


def _hit(n: int = 1) -> dict:
    return {
        "resultId": f"r{n}",
        "threadKey": "tel:1",
        "previewPlain": "hello",
        "preview": "hello",
        "stamp": "",
        "eventId": n,
        "guid": "",
        "phone": "1",
    }


def test_clear_empty_field_leaves_search_inactive(store):
    store._search_pending = "hello"
    store._search_query = "hello"
    store._search_model.reload([{
        "resultId": "r1", "threadKey": "tel:1", "name": "A",
        "preview": "hello", "richPreview": "hello", "stamp": "",
        "unread": 0, "pinned": False, "avatar": "", "initials": "A",
        "eventId": 1, "guid": "",
    }])
    assert store.searchActive

    store.setSearchQuery("")
    assert store._search_pending == ""
    assert store._search_query == ""
    assert not store.searchActive
    assert store._search_model.rowCount() == 0
    assert store._search_inflight is False


def test_apply_after_clear_is_ignored(store):
    store.setSearchQuery("")
    gen_before = store._search_gen
    store._apply_search_hits("hello", [_hit()], gen=gen_before)
    assert not store.searchActive
    assert store._search_model.rowCount() == 0


def test_apply_requires_matching_pending(store):
    store._search_pending = "other"
    store._search_gen = 3
    store._apply_search_hits("hello", [_hit()], gen=3)
    assert store._search_model.rowCount() == 0
    assert store._search_query == ""


def test_stale_gen_apply_is_ignored(store):
    store._search_pending = "hello"
    store._search_gen = 5
    store._apply_search_hits("hello", [_hit()], gen=4)
    assert store._search_model.rowCount() == 0

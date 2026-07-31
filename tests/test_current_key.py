"""`ThreadStore.currentKey` is what the sidebar highlights on.

Both sidebar sections select on this: the pinned grid is a Repeater with no
`currentIndex`, and the thread list stopped using its own index because opening
a chat from the grid never updated it — which left the previously opened row
lit alongside the pinned tile.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject

from iphonebridge.qtui.models import ThreadStore


@pytest.fixture
def store():
    s = ThreadStore.__new__(ThreadStore)
    QObject.__init__(s)  # signals need it; ThreadStore.__init__ would hit D-Bus
    s._threads = {"a": {"key": "a", "messages": []}, "b": {"key": "b", "messages": []}}
    s._current = None
    return s


def test_no_open_thread_is_empty_string(store):
    """QML compares against a string role; None would come through as
    "undefined" and match nothing predictably."""
    assert store.currentKey == ""


def test_tracks_the_open_thread(store):
    store._current = "b"
    assert store.currentKey == "b"


def test_peer_changed_is_the_notify_signal(store):
    """Without a notify signal the binding evaluates once, at delegate
    creation — before any thread is open — and never refreshes."""
    seen = []
    store.peerChanged.connect(lambda: seen.append(store.currentKey))
    store._current = "a"
    store.peerChanged.emit()
    assert seen == ["a"]

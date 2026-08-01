"""Argument order for the iMessage verbs the UI can invoke.

`ThreadStore._act` splices the recipient in front and the two callbacks
behind, so every one of these has a different middle. Getting the order wrong
produces a plausible-looking D-Bus call that reacts with the wrong emoji or
edits the wrong message — a class of bug that no amount of QML review finds.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from iphonebridge.qtui.models import ThreadStore

PEER = "+12155550150"
GUID = "GUID-1"


class FakeClient:
    def __init__(self):
        self.calls = []

    def _record(self, name):
        def fn(*args, **kw):
            # Drop the two trailing callbacks; they're not part of the shape
            # under test.
            self.calls.append((name, args[:-2]))
        return fn

    def __getattr__(self, name):
        return self._record(name)


@pytest.fixture
def store():
    """A ThreadStore with one open thread and no Qt plumbing running."""
    # ThreadStore.__new__, not object.__new__: QObject subclasses reject the
    # latter. Bypassing __init__ still avoids the D-Bus and file watchers.
    s = ThreadStore.__new__(ThreadStore)
    s._client = FakeClient()
    s._current = "them"
    s._threads = {
        "them": {"phone": PEER, "messages": [{"guid": GUID, "body": "hi there"}]}
    }
    s._by_guid = {GUID: s._threads["them"]["messages"][0]}
    return s


def test_react_passes_guid_kind_then_snippet(store):
    store.react(GUID, "Heart")
    assert store._client.calls == [("react", (PEER, GUID, "Heart", "hi there"))]


def test_react_sends_emoji_through_unchanged(store):
    """An emoji tapback rides the same argument as a verb name."""
    store.react(GUID, "🎉")
    assert store._client.calls[0][1][2] == "🎉"


def test_react_paints_the_badge_before_the_wire_acks(store):
    """Apple does not echo a reaction back to the sender, so the badge has
    to appear from the act of sending or the desktop never shows it."""
    store._message_model = None  # _refresh_reactions no-ops without a model
    store._current = "them"
    store.react(GUID, "Heart")
    msg = store._threads["them"]["messages"][0]
    assert msg["reactions"] == {"me": "❤️"}


def test_react_emoji_paints_the_literal_emoji(store):
    store._message_model = None
    store._current = "them"
    store.react(GUID, "🎉")
    msg = store._threads["them"]["messages"][0]
    assert msg["reactions"] == {"me": "🎉"}


def test_reply_puts_body_before_the_target(store):
    """SendReply is (recipient, body, guid, target_text) — the odd one out.

    The target's own text goes along because Apple's reply format encodes a
    character range over it; without it the reply is delivered as an ordinary
    message.
    """
    store.replyTo(GUID, "  on my way  ")
    assert store._client.calls == [
        ("send_reply", (PEER, "on my way", GUID, "hi there"))]


def test_edit_puts_the_target_before_the_new_text(store):
    store.editMessage(GUID, "  fixed typo ")
    assert store._client.calls == [("edit_message", (PEER, GUID, "fixed typo"))]


def test_unsend_needs_only_the_target(store):
    store.unsendMessage(GUID)
    assert store._client.calls == [("unsend_message", (PEER, GUID))]


@pytest.mark.parametrize(
    "action,args",
    [("react", ("", "Heart")), ("replyTo", ("", "x")),
     ("editMessage", ("", "x")), ("unsendMessage", ("",))],
)
def test_no_guid_means_no_call(store, action, args):
    """MAP messages have no guid, and every verb needs one to name a target."""
    getattr(store, action)(*args)
    assert store._client.calls == []


def test_empty_body_is_not_sent_as_a_reply_or_edit(store):
    """Enter on an empty composer must not fire a blank edit."""
    store.replyTo(GUID, "   ")
    store.editMessage(GUID, "")
    assert store._client.calls == []


def test_actions_are_dropped_when_no_thread_is_open(store):
    store._current = ""
    store.react(GUID, "Heart")
    assert store._client.calls == []

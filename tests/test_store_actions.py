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


def test_group_reply_targets_the_participant_set(store):
    """Groups have phone=None; reply must still reach every other member.

    `_peer` used to return None for groups, so reply/react/edit all no-op'd
    silently — the exact failure reported for "the fucky".
    """
    gkey = (
        "imessage-group:tel:+12155550001,tel:+12155550002,tel:+12155550003"
    )
    store._current = gkey
    store._threads[gkey] = {
        "key": gkey,
        "phone": None,
        "is_group": True,
        "messages": [{"guid": GUID, "body": "who is free?"}],
    }
    store._by_guid = {GUID: store._threads[gkey]["messages"][0]}

    store.replyTo(GUID, "I am")
    assert store._client.calls == [
        ("send_reply", (
            "tel:+12155550001,tel:+12155550002,tel:+12155550003",
            "I am",
            GUID,
            "who is free?",
        ))
    ]


def test_group_send_uses_the_same_participant_set(store):
    gkey = "imessage-group:tel:+12155550001,tel:+12155550002"
    store._current = gkey
    store._threads[gkey] = {
        "key": gkey, "phone": None, "is_group": True, "messages": [],
    }
    store.send("hello group")
    assert store._client.calls == [
        ("send_message", (
            "tel:+12155550001,tel:+12155550002", "hello group",
        ))
    ]


def test_group_without_imessage_key_cannot_send(store):
    """Legacy/unknown group keys have no participant list we can address."""
    store._current = "any;+;chat123"
    store._threads["any;+;chat123"] = {
        "key": "any;+;chat123", "phone": None, "is_group": True,
        "messages": [{"guid": GUID, "body": "x"}],
    }
    store._by_guid = {GUID: store._threads["any;+;chat123"]["messages"][0]}
    store.send("nope")
    store.replyTo(GUID, "nope")
    assert store._client.calls == []

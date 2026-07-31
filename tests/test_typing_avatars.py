"""Per-person typing indicators for group chats.

Typing used to be a single bool per thread. Groups need to know *who* is
typing so the footer can stack their avatars beside the dots bubble.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject

from iphonebridge.qtui import models as models_mod
from iphonebridge.qtui.models import ThreadStore, _TYPING_TIMEOUT_SEC


def _store() -> ThreadStore:
    s = ThreadStore.__new__(ThreadStore)
    QObject.__init__(s)
    s._threads = {}
    s._by_phone = {}
    s._current = None
    s._typing_until = {}
    s._contacts = MagicMock()
    s._contacts.resolve.return_value = None
    s._contacts.resolve_photo.return_value = None
    return s


def test_set_typing_tracks_person_not_just_thread():
    s = _store()
    s._by_phone["12155550103"] = "alice"
    s._threads["alice"] = {
        "key": "alice", "is_group": False, "messages": [], "phone": "+12155550103",
    }
    s._current = "alice"

    s._set_typing("+12155550103", True)
    assert "12155550103" in s._typing_until["alice"]
    assert s.peerTyping is True
    # 1:1: no avatar stack
    assert s.typingAvatars == []

    s._set_typing("+12155550103", False)
    assert "alice" not in s._typing_until
    assert s.peerTyping is False


def test_group_typing_lists_avatars_and_supports_multiple():
    s = _store()
    gkey = "group:abc"
    s._threads[gkey] = {
        "key": gkey,
        "is_group": True,
        "messages": [
            {"sender_phone": "+12155550103", "sender_name": "Alice",
             "outgoing": False, "body": "hi", "ts": ""},
            {"sender_phone": "+12155550104", "sender_name": "Bob",
             "outgoing": False, "body": "yo", "ts": ""},
        ],
    }
    s._current = gkey
    s._contacts.resolve.side_effect = lambda h: {
        "12155550103": "Alice", "+12155550103": "Alice",
        "12155550104": "Bob", "+12155550104": "Bob",
    }.get(h) or s._contacts.resolve.return_value

    # Avoid filesystem work in circular_avatar during the test.
    original = models_mod.circular_avatar
    models_mod.circular_avatar = MagicMock(return_value=None)
    try:
        s._set_typing("+12155550103", True)
        s._set_typing("+12155550104", True)
        assert s.peerTyping is True
        avatars = s.typingAvatars
        assert len(avatars) == 2
        initials = {a["initials"] for a in avatars}
        # _initials("Alice") → "A" (single token, first two chars → "AL")
        assert "AL" in initials
        assert "B" in initials or "BO" in initials
        for a in avatars:
            assert "avatar" in a

        s._set_typing("+12155550103", False)
        assert len(s.typingAvatars) == 1
        s._set_typing("+12155550104", False)
        assert s.typingAvatars == []
        assert s.peerTyping is False
    finally:
        models_mod.circular_avatar = original


def test_expire_typing_is_per_person():
    s = _store()
    gkey = "group:abc"
    s._threads[gkey] = {
        "key": gkey, "is_group": True,
        "messages": [
            {"sender_phone": "+12155550103", "sender_name": "Alice"},
            {"sender_phone": "+12155550104", "sender_name": "Bob"},
        ],
    }
    s._current = gkey
    now = time.monotonic()
    s._typing_until[gkey] = {
        "12155550103": now - 1.0,  # already stale
        "12155550104": now + _TYPING_TIMEOUT_SEC,
    }
    s._expire_typing()
    people = s._typing_until.get(gkey) or {}
    assert "12155550103" not in people
    assert "12155550104" in people
    assert s.peerTyping is True


def test_group_open_prefers_group_over_one_to_one():
    """When viewing a group, a member's typing lights the group, not their DM."""
    s = _store()
    gkey = "group:abc"
    s._threads[gkey] = {
        "key": gkey, "is_group": True,
        "messages": [
            {"sender_phone": "+12155550103", "sender_name": "Alice"},
        ],
    }
    s._by_phone["12155550103"] = "alice-dm"
    s._threads["alice-dm"] = {
        "key": "alice-dm", "is_group": False, "messages": [], "phone": "+12155550103",
    }
    s._current = gkey
    s._set_typing("+12155550103", True)
    assert "12155550103" in s._typing_until[gkey]
    assert "alice-dm" not in s._typing_until

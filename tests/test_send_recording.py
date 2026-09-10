"""Anything that puts text in front of the other person lands in our history.

`SendReply` sent the message and returned its guid, but never called the
`on_sent` hook — so the reply reached the recipient while our own conversation
showed nothing. From the sending side that is indistinguishable from replying
being broken, which is exactly how it was reported.
"""
from __future__ import annotations

import pytest

dbus = pytest.importorskip("dbus")

from verdigris.dbus_service import MessagesService  # noqa: E402


class FakeIMessage:
    """Stands in for the helper client; records what it was asked to send."""

    def __init__(self):
        self.sent = []

    def send(self, participants, text, **kw):
        self.sent.append((participants, text, kw))
        return "GUID-1"


def service():
    """A MessagesService with only the pieces these paths touch."""
    svc = MessagesService.__new__(MessagesService)
    svc.imessage = FakeIMessage()
    svc.recorded = []
    svc._on_sent = lambda r, b, h, reply="": svc.recorded.append(
        (r, b, h, reply))
    # Read receipts are a separate decision with their own tests; keep them
    # out of the way so a failure here is unambiguous.
    svc._read_receipt_on_send = lambda recipient: None
    svc._imessage_call = lambda what, fn, *a, **kw: fn(*a, **kw)
    svc._imessage = lambda: svc.imessage
    svc._participants = lambda r: [r]
    return svc


def test_a_reply_is_recorded_in_our_own_history():
    svc = service()
    svc.SendReply("+12155550150", "sounds good", "TARGET-GUID")

    assert svc.recorded == [
        ("+12155550150", "sounds good", "GUID-1", "TARGET-GUID")
    ], (
        "a reply has to reach our own conversation, and has to keep pointing "
        "at what it replies to — without the target it lands as an ordinary "
        "message, which looks the same as replying not working"
    )


def test_a_reply_still_carries_its_target():
    """Recording it must not cost the threading that makes it a reply."""
    svc = service()
    svc.SendReply("+12155550150", "sounds good", "TARGET-GUID")

    _, _, kwargs = svc.imessage.sent[0]
    assert kwargs.get("reply_guid") == "TARGET-GUID"


def test_a_failing_recorder_does_not_fail_the_send():
    """The message is already gone; bookkeeping must not raise over it."""
    svc = service()

    def boom(*_a):
        raise RuntimeError("disk full")

    svc._on_sent = boom
    assert svc.SendReply("+12155550150", "sounds good", "T") == "GUID-1"


def test_group_send_splits_participants():
    """A group recipient is many handles, not one mangled address."""
    svc = service()
    # Real _participants: the stub above collapsed to [r], which would hide
    # a regression that treated the comma list as a single handle.
    from verdigris.dbus_service import MessagesService
    svc._participants = lambda r: MessagesService._participants(svc, r)

    recipients = "tel:+12155550001,tel:+12155550002,tel:+12155550003"
    svc.Send(recipients, "hey all")

    parts, text, _kw = svc.imessage.sent[0]
    assert text == "hey all"
    assert parts == [
        "tel:+12155550001", "tel:+12155550002", "tel:+12155550003",
    ]


def test_group_reply_splits_participants_and_keeps_threading():
    svc = service()
    from verdigris.dbus_service import MessagesService
    svc._participants = lambda r: MessagesService._participants(svc, r)

    recipients = "tel:+12155550001,tel:+12155550002"
    svc.SendReply(recipients, "same", "TARGET-G", "who is free?")

    parts, text, kw = svc.imessage.sent[0]
    assert parts == ["tel:+12155550001", "tel:+12155550002"]
    assert text == "same"
    assert kw.get("reply_guid") == "TARGET-G"
    assert svc.recorded == [
        (recipients, "same", "GUID-1", "TARGET-G")
    ]


def test_group_send_does_not_map_fallback_on_imessage_failure():
    """MAP only addresses one phone. Falling back after a group iMessage
    failure looked like success (transfer path) while nothing reached the
    chat — and IDS was previously looking up the whole comma list as one
    handle when the split was missing.
    """
    svc = service()
    from verdigris.dbus_service import MessagesService
    svc._participants = lambda r: MessagesService._participants(svc, r)
    svc.sessions = type("S", (), {"map": object(), "map_path": "/map"})()

    def boom(*_a, **_k):
        raise RuntimeError("IDS returned zero keys")

    svc.imessage.send = boom
    with pytest.raises(dbus.exceptions.DBusException) as ei:
        svc.Send("tel:+12155550001,tel:+12155550002", "hey")
    assert "SendFailed" in ei.value.get_dbus_name()
    assert svc.recorded == []

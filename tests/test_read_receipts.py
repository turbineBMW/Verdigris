"""Read receipts ride along with outgoing messages, and nothing else.

The rule these pin down: looking at a conversation must never tell the sender
anything, but replying to one may — because the reply already did.

The risk being guarded against is specific. `ThreadStore._mark_read` (local
badge bookkeeping, runs on every thread open) and `Messages1.MarkRead` (sends
a receipt to Apple) have nearly the same name, and wiring the first to the
second is the obvious-looking way to implement read indicators. It would also
silently start broadcasting to everyone the user has ever messaged.
"""
from __future__ import annotations

import types

import pytest

from iphonebridge import config
from iphonebridge.dbus_service import MessagesService


class FakeIMessage:
    """Records what got sent to Apple, so tests can assert on disclosure."""

    def __init__(self):
        self.sent = []
        self.reactions = []
        self.read_receipts = []

    def send(self, participants, body, **kw):
        self.sent.append((participants, body, kw))
        return "guid-1"

    def react(self, participants, target_guid, **kw):
        self.reactions.append((participants, target_guid, kw))
        return "guid-2"

    def mark_read(self, participants):
        self.read_receipts.append(participants)
        return "ok"


@pytest.fixture
def svc():
    """A MessagesService with the D-Bus machinery bypassed.

    `__init__` would claim a bus name and export an object; neither is needed
    to exercise the send/receipt logic, and both would fail without a bus.
    """
    s = object.__new__(MessagesService)
    s.imessage = FakeIMessage()
    s.sessions = types.SimpleNamespace(map=None, map_path=None)
    s._on_sent = None
    return s


@pytest.fixture(autouse=True)
def receipts_on(monkeypatch):
    monkeypatch.setattr(config, "SEND_READ_RECEIPTS", True)


def test_sending_discloses(svc):
    svc.Send("+12155550150", "on my way")
    assert svc.imessage.read_receipts == [["tel:+12155550150"]]


def test_replying_discloses(svc):
    svc.SendReply("+12155550150", "this one", "target-guid")
    assert svc.imessage.read_receipts == [["tel:+12155550150"]]


def test_reacting_discloses(svc):
    """A tapback names the message it read, so it admits the most of all."""
    svc.React("+12155550150", "target-guid", "Heart", "their message")
    assert svc.imessage.read_receipts == [["tel:+12155550150"]]


def test_nothing_is_disclosed_without_an_outgoing_message(svc):
    """The whole point: no send, no receipt.

    Viewing a thread runs local bookkeeping only and never reaches here.
    """
    assert svc.imessage.read_receipts == []


def test_master_switch_suppresses_the_receipt_but_not_the_send(svc, monkeypatch):
    monkeypatch.setattr(config, "SEND_READ_RECEIPTS", False)
    svc.Send("+12155550150", "still goes out")
    assert svc.imessage.sent, "disabling receipts must not block the message"
    assert svc.imessage.read_receipts == []


def test_explicit_markread_respects_the_switch(svc, monkeypatch):
    monkeypatch.setattr(config, "SEND_READ_RECEIPTS", False)
    svc.MarkRead("+12155550150")
    assert svc.imessage.read_receipts == []


def test_a_failed_receipt_does_not_fail_the_send(svc):
    """The receipt fires after the message is already gone.

    Propagating the error would report a delivered message as failed and
    invite the user to send it a second time.
    """
    def boom(participants):
        raise RuntimeError("APNs hiccup")

    svc.imessage.mark_read = boom
    assert svc.Send("+12155550150", "delivered fine") == "guid-1"


def test_group_recipients_all_receive_the_receipt(svc):
    svc.Send("+12155550150,me@icloud.com", "hi both")
    assert svc.imessage.read_receipts == [
        ["tel:+12155550150", "mailto:me@icloud.com"]
    ]

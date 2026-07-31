"""The guid-keyed metadata and state channel from daemon to UI.

Two things are pinned here:

  * A message payload carries enough for the UI to *act* on the message —
    principally `im_guid`, without which there is no tapback, reply, edit or
    unsend, because every one of those verbs names its target by guid.
  * Delivery, read and typing updates reach the bus at all. They used to be
    parsed and then dropped on the floor.
"""
from __future__ import annotations

import json

import pytest

from iphonebridge.dbus_service import _extras_dict, _variant_dict
from iphonebridge.imessage.bridge import IMessageExtras, translate

MY_HANDLES = ["mailto:me@icloud.com", "tel:+12155550100"]
THEM = "tel:+12155550150"


def inst(message, *, sender=THEM, guid="GUID-1"):
    """A helper `message` event shaped the way the Rust side emits them."""
    return {
        "event": "message",
        "inst": {
            "id": guid,
            "sender": sender,
            "conversation": {"participants": [sender, MY_HANDLES[0]]},
            "message": message,
            "sent_timestamp": "1753900000000",
        },
    }


# ---- extras on the wire -------------------------------------------------

def test_guid_survives_to_the_payload():
    """Everything the UI can do to a message is keyed by this."""
    extras = IMessageExtras(guid="GUID-1", outgoing=False, sender=THEM)
    assert _extras_dict(extras)["im_guid"] == "GUID-1"


def test_structured_values_round_trip_as_json():
    """a{sv} can't nest, so lists travel as JSON and must parse back.

    Before this, `str()` rendered them as Python reprs — single-quoted and
    unparseable by anything on the far side.
    """
    extras = IMessageExtras(
        guid="G",
        outgoing=False,
        chat_participants=[THEM, MY_HANDLES[0]],
        attachments=[{"name": "IMG_1.HEIC", "size": 12}],
    )
    payload = _variant_dict(_extras_dict(extras))
    assert json.loads(payload["im_participants"]) == [THEM, MY_HANDLES[0]]
    assert json.loads(payload["im_attachments"]) == [{"name": "IMG_1.HEIC", "size": 12}]


def test_none_fields_become_empty_strings_not_the_word_none():
    """`str(None)` would put a literal 'None' on the bus for the UI to render."""
    payload = _variant_dict(_extras_dict(IMessageExtras(guid="G", outgoing=False)))
    assert payload["im_reply_to_guid"] == ""
    assert payload["im_chat_name"] == ""


def test_reply_target_reaches_the_payload():
    t = translate(inst({"Message": {"parts": [], "reply_guid": "PARENT"}}), MY_HANDLES)
    assert _extras_dict(t.extras)["im_reply_to_guid"] == "PARENT"


# ---- state changes ------------------------------------------------------

class FakeService:
    def __init__(self):
        self.states = []

    def emit_message_state(self, guid, state, handle, timestamp):
        self.states.append((guid, state, handle))


@pytest.fixture
def daemon():
    """A bare object carrying only what _emit_state touches."""
    from iphonebridge.daemon import Daemon

    d = object.__new__(Daemon)
    d._dbus_service = FakeService()
    d._imessage_handles = list(MY_HANDLES)
    d.sinks = []
    return d


@pytest.mark.parametrize(
    "apple_name,expected",
    [("Delivered", "delivered"), ("Read", "read"), ("MessageReadOnDevice", "read")],
)
def test_receipts_are_emitted(daemon, apple_name, expected):
    t = translate(inst(apple_name), MY_HANDLES)
    assert t.event is None, "a receipt must not create a message row"
    daemon._emit_state(t)
    assert daemon._dbus_service.states == [("GUID-1", expected, THEM)]


def test_typing_start_and_stop(daemon):
    daemon._emit_state(translate(inst({"Typing": [True]}), MY_HANDLES))
    daemon._emit_state(translate(inst({"Typing": [False]}), MY_HANDLES))
    assert [s[1] for s in daemon._dbus_service.states] == ["typing", "typing_stopped"]


def test_state_routes_to_the_other_party_not_ourselves(daemon):
    """Our own handle is in `participants`, so `participants[0]` is a trap.

    A typing indicator keyed to our own handle would surface in the wrong
    conversation, or in none at all.
    """
    daemon._emit_state(translate(inst({"Typing": [True]}), MY_HANDLES))
    assert daemon._dbus_service.states[0][2] == THEM


def test_unknown_receipt_is_ignored_not_emitted(daemon):
    """Apple adds message types; an unrecognised one must not become a state."""
    from iphonebridge.imessage.bridge import Translated

    daemon._emit_state(Translated(event=None, extras=None, receipt="SomethingNew"))
    assert daemon._dbus_service.states == []

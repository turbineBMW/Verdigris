"""Which conversation an iMessage lands in.

Both rules here were regressions found in live use, and both have the same
shape: our own handle appears in the participant list, so any code that picks
a "peer" without excluding it files the message under the wrong person.
"""
from __future__ import annotations

from iphonebridge.imessage.bridge import translate

ME_EMAIL = "mailto:me@icloud.com"
ME_PHONE = "tel:+12155550100"
MY_HANDLES = [ME_EMAIL, ME_PHONE]
ALICE = "tel:+12155550150"
BOB = "tel:+12155542060"


def msg(sender, participants, *, guid="G1", cv_name=None):
    return {
        "event": "message",
        "inst": {
            "id": guid,
            "sender": sender,
            "conversation": {"participants": participants, "cv_name": cv_name},
            "message": {"Message": {"parts": [
                {"part": {"Text": ["hello", {}]}, "idx": None, "ext": None}
            ]}},
            "sent_timestamp": "1753900000000",
        },
    }


# ---- group chats --------------------------------------------------------

def test_group_message_carries_a_chat_key():
    """Without one, each participant's messages scatter into 1:1 threads."""
    t = translate(msg(ALICE, [ALICE, BOB, ME_EMAIL]), MY_HANDLES)
    assert t.event.chat_guid


def test_a_group_key_is_the_same_whoever_sends():
    """The whole point: one conversation, not one per speaker."""
    a = translate(msg(ALICE, [ALICE, BOB, ME_EMAIL]), MY_HANDLES)
    b = translate(msg(BOB, [BOB, ME_EMAIL, ALICE]), MY_HANDLES)
    assert a.event.chat_guid == b.event.chat_guid


def test_the_group_key_ignores_which_of_our_handles_was_addressed():
    """A message to our email and one to our number are the same group."""
    a = translate(msg(ALICE, [ALICE, BOB, ME_EMAIL]), MY_HANDLES)
    b = translate(msg(ALICE, [ALICE, BOB, ME_PHONE]), MY_HANDLES)
    assert a.event.chat_guid == b.event.chat_guid


def test_one_to_one_has_no_chat_key():
    """A direct message must thread by phone number.

    That's what merges it with everything MAP and the backup already filed
    under that person; a synthetic key would split the conversation in two.
    """
    t = translate(msg(ALICE, [ALICE, ME_EMAIL]), MY_HANDLES)
    assert t.event.chat_guid is None


def test_group_name_is_carried_when_the_chat_has_one():
    t = translate(msg(ALICE, [ALICE, BOB, ME_EMAIL], cv_name="Weekend"), MY_HANDLES)
    assert t.event.chat_name == "Weekend"


# ---- direction ----------------------------------------------------------

def test_our_own_message_threads_under_the_recipient():
    """Not under ourselves.

    Our handle is in `participants`, so taking the first entry filed sent
    messages into a conversation with ourselves.
    """
    t = translate(msg(ME_EMAIL, [ME_EMAIL, ALICE]), MY_HANDLES)
    assert t.event.kind == "sms_sent"
    assert t.event.sender_phone_norm == "12155550150"


def test_an_incoming_message_threads_under_the_sender():
    t = translate(msg(ALICE, [ALICE, ME_EMAIL]), MY_HANDLES)
    assert t.event.kind == "sms_received"
    assert t.event.sender_phone_norm == "12155550150"


# ---- guid ---------------------------------------------------------------

def test_the_guid_reaches_the_event_not_just_the_extras():
    """It has to be on the event to survive being written to events.jsonl.

    Otherwise a message stops being actionable — no tapback, no reply, no
    delivery caption — as soon as the app restarts and reloads history.
    """
    t = translate(msg(ALICE, [ALICE, ME_EMAIL], guid="ABC-123"), MY_HANDLES)
    assert t.event.guid == "ABC-123"
    assert "guid" in t.event.to_dict()

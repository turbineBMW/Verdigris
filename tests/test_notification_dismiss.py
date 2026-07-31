"""Opening a conversation has to take its notifications off the screen.

The sink's only automatic close is a BlueZ `Message1.Read` property change,
which is a MAP object. An iMessage arrives with no such object to watch, so on
the transport that now carries most messages nothing ever closed a popup and
they accumulated until dismissed by hand.

The two halves are tested apart from D-Bus: which handles the UI sends, and
whether the sink can match them to the popups it opened.
"""
from __future__ import annotations

from iphonebridge.qtui.models import ThreadStore
from iphonebridge.sinks.libnotify import LibnotifySink

_key = LibnotifySink._peer_key


def test_the_same_person_keys_the_same_from_either_side():
    """The UI knows a sender by what the message carried, the popup by what
    the transport gave us, and for one person those differ."""
    assert _key("tel:+12155550101") == _key("+1 (215) 555-0101")
    assert _key("2155550101") == _key("+12155550101")


def test_email_handles_survive():
    """normalize_phone would strip an iMessage address to nothing, and every
    such popup would then key to None and never close."""
    assert _key("Someone@Example.com") == "someone@example.com"


def test_blank_handles_key_to_nothing():
    for value in ("", "   ", None):
        assert _key(value) is None


def _store(messages: list[dict], phone: str | None = "+12155550101"):
    store = ThreadStore.__new__(ThreadStore)
    store._threads = {}
    store._read_marks = {}
    store._save_read_marks = lambda: None
    sent: list[str] = []
    store._client = type(
        "C", (), {"dismiss_notifications": lambda self, p: sent.append(p)}
    )()
    thread = {"key": "k", "messages": messages, "phone": phone, "unread": 3}
    return store, thread, sent


def _msg(sender: str | None, outgoing: bool = False) -> dict:
    return {"sender_phone": sender, "outgoing": outgoing, "ts": "2026-07-31"}


def test_reading_a_thread_dismisses_its_popups():
    store, thread, sent = _store([_msg("+12155550101")])
    store._mark_read(thread)
    assert sent and "+12155550101" in sent[0]


def test_a_group_clears_every_member_who_spoke():
    """A group notifies under whoever sent the message, so dismissing only the
    thread's own handle leaves the rest of the popups on screen."""
    store, thread, sent = _store(
        [_msg("+12155550001"), _msg("+12155550002"), _msg("+12155550003")],
        phone=None,
    )
    store._mark_read(thread)
    for handle in ("+12155550001", "+12155550002", "+12155550003"):
        assert handle in sent[0]


def test_outgoing_messages_contribute_no_handles():
    """We never raise a popup for our own message, so our own number must not
    reach the dismiss call — it would be asking to close the other party's
    popups under our handle. The thread's own peer is used instead."""
    store, thread, sent = _store([_msg("+12155550150", outgoing=True)])
    store._mark_read(thread)
    assert sent == ["+12155550101"]


def test_the_handle_list_is_bounded():
    """A long thread would otherwise put hundreds of handles on the wire every
    time you switched to it."""
    store, thread, sent = _store(
        [_msg(f"+1215555{n:04d}") for n in range(500)], phone=None)
    store._mark_read(thread)
    assert len(sent[0].split(",")) <= 16


def test_a_thread_with_no_incoming_handles_falls_back_to_its_own():
    store, thread, sent = _store([_msg(None)])
    store._mark_read(thread)
    assert sent == ["+12155550101"]


def test_a_failing_daemon_does_not_break_opening_a_thread():
    """Dismissal is cosmetic; a daemon that isn't there must not stop the UI
    from marking the thread read."""
    store, thread, _ = _store([_msg("+12155550101")])

    def boom(_self, _peers):
        raise RuntimeError("daemon not reachable")

    store._client = type("C", (), {"dismiss_notifications": boom})()
    store._mark_read(thread)
    assert thread["unread"] == 0

"""Self-chat: Apple's same-guid loopback becomes a grey incoming bubble.

Texting your own number produces two deliveries of one guid — the send and
the echo. For a normal chat the echo is dropped (you don't want your own
message twice). For self-chat the echo is the "texted me back" copy, so we
keep the original blue send and surface the second as incoming.
"""
from __future__ import annotations

from verdigris.daemon import Daemon
from verdigris.imessage.bridge import translate

MY = ["mailto:me@icloud.com", "tel:+12155550100"]
SELF = "tel:+12155550100"
THEM = "tel:+12155550150"
GUID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"


def _inst(message, *, sender, participants, guid=GUID):
    return {
        "event": "message",
        "inst": {
            "id": guid,
            "sender": sender,
            "conversation": {"participants": participants},
            "message": message,
            "sent_timestamp": "1753900000000",
        },
    }


def _daemon(seen=None):
    d = object.__new__(Daemon)
    d._imessage_handles = set(MY)
    d._imessage_seen = dict.fromkeys(seen or [])
    d._imessage_extras = {}
    d._SEEN_LIMIT = 100
    return d


def test_self_chat_duplicate_becomes_incoming_echo():
    d = _daemon(seen=[GUID])
    t = translate(
        _inst({"Message": {"parts": [{"part": {"Text": ["hi", {}]}}]}},
              sender=SELF, participants=[SELF]),
        MY,
    )
    assert t is not None and t.event is not None
    # Pretend the guid filter just hit.
    assert GUID in d._imessage_seen
    assert d._is_self_chat(t)
    d._as_self_echo(t)
    assert t.event.kind == "sms_received"
    assert t.event.guid is None
    assert t.event.handle.endswith("#self-echo")
    assert t.event.is_read is True


def test_normal_chat_is_not_self():
    d = _daemon()
    t = translate(
        _inst({"Message": {"parts": [{"part": {"Text": ["hi", {}]}}]}},
              sender=THEM, participants=[THEM, MY[0]]),
        MY,
    )
    assert d._is_self_chat(t) is False


def test_self_chat_with_only_our_handles():
    d = _daemon()
    t = translate(
        _inst({"Message": {"parts": [{"part": {"Text": ["note", {}]}}]}},
              sender=SELF, participants=[SELF, MY[0]]),
        MY,
    )
    assert d._is_self_chat(t) is True


def test_remember_guid_seeds_seen_for_local_sends():
    """Local Send must mark the guid so the Apple echo is recognised."""
    d = _daemon()
    d._remember_guid(GUID)
    assert GUID in d._imessage_seen


def test_own_number_is_self_recipient():
    d = _daemon()
    # SELF is tel:+12155550100 in this fixture (one of MY).
    assert d._is_self_recipient("+12155550100") is True
    assert d._is_self_recipient("tel:+12155550100") is True
    assert d._is_self_recipient("+15551234567") is False


def test_synthetic_self_echo_is_incoming_without_guid():
    from verdigris.events import sms_sent_event

    sent = sms_sent_event("+12155550150", "hi", guid=GUID,
                          transfer_path=GUID)
    echo = Daemon._synthetic_self_echo(sent, GUID)
    assert echo.kind == "sms_received"
    assert echo.handle == f"{GUID}#self-echo"
    assert echo.guid is None
    assert echo.body == "hi"

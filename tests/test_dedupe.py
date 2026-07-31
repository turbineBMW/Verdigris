"""Cross-transport dedupe.

The cases here are taken from real traffic observed in events.jsonl, not
invented: the same message genuinely arrived as both an iMessage and a MAP
notification, and MAP genuinely echoed back messages we had just sent.
"""
from __future__ import annotations

from iphonebridge.dedupe import (
    TRANSPORT_IMESSAGE,
    TRANSPORT_MAP,
    TRANSPORT_SENT,
    CrossTransportDeduper,
    normalize_body,
    transport_of,
)
from iphonebridge.events import SmsEvent


def ev(body, *, raw_type="SMS_GSM", sender="12155550150", kind="sms_received"):
    return SmsEvent(
        kind=kind,
        handle=f"h-{body}-{raw_type}",
        sender_phone=sender,
        sender_phone_norm=sender,
        contact_name=None,
        body=body,
        timestamp=None,
        is_read=False,
        raw_status="notification",
        raw_type=raw_type,
    )


def test_transport_classification():
    assert transport_of(ev("x", raw_type="iMessage")) == TRANSPORT_IMESSAGE
    assert transport_of(ev("x", raw_type="sms_sent")) == TRANSPORT_SENT
    assert transport_of(ev("x", raw_type="SMS_GSM")) == TRANSPORT_MAP
    # An unknown or missing raw_type is treated as MAP, which is the
    # conservative choice: MAP is the transport that predates all of this.
    assert transport_of(ev("x", raw_type="")) == TRANSPORT_MAP


def test_same_message_over_both_transports_is_deduped():
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev("threaded reply", raw_type="iMessage")) is False
    assert d.is_duplicate(ev("threaded reply", raw_type="SMS_GSM")) is True


def test_dedupe_works_in_either_arrival_order():
    """MAP is sometimes first, sometimes second — neither may win twice."""
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev("hello", raw_type="SMS_GSM")) is False
    assert d.is_duplicate(ev("hello", raw_type="iMessage")) is True


def test_map_echo_of_our_own_send_is_deduped():
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev("verb suite base", raw_type="sms_sent", kind="sms_sent")) is False
    assert d.is_duplicate(ev("verb suite base", raw_type="SMS_GSM")) is True


def test_repeated_message_on_one_transport_is_kept():
    """The rule that makes this safe: never match within a transport.

    Sending "ok" twice must produce two rows.
    """
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev("ok", raw_type="SMS_GSM")) is False
    assert d.is_duplicate(ev("ok", raw_type="SMS_GSM")) is False
    assert d.is_duplicate(ev("ok", raw_type="iMessage")) is True
    # ...and a third MAP copy is still its own message, not a duplicate of
    # the iMessage one we just suppressed.
    assert d.is_duplicate(ev("ok", raw_type="SMS_GSM")) is False


def test_whitespace_and_quote_differences_still_match():
    """MAP's synthesized tapback text is spaced and quoted differently.

    Observed: MAP wrote 'Reacted 🎉 to  “x”' (two spaces) where the native
    transport produces one.
    """
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev('Reacted 🎉 to “dbus routed”', raw_type="iMessage")) is False
    assert d.is_duplicate(ev('Reacted 🎉 to  "dbus routed"', raw_type="SMS_GSM")) is True


def test_empty_body_is_never_suppressed():
    """'Removed a laugh from “”' has nothing to match on.

    A duplicate row beats a dropped message.
    """
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev("", raw_type="iMessage")) is False
    assert d.is_duplicate(ev("", raw_type="SMS_GSM")) is False


def test_missing_sender_still_matches():
    """MAP omitted the sender on every self-addressed message in testing."""
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev("testing from iphone", raw_type="iMessage")) is False
    assert (
        d.is_duplicate(ev("testing from iphone", raw_type="SMS_GSM", sender=None))
        is True
    )


def test_different_senders_are_not_deduped():
    d = CrossTransportDeduper()
    assert d.is_duplicate(ev("same text", raw_type="iMessage", sender="15551110000")) is False
    assert d.is_duplicate(ev("same text", raw_type="SMS_GSM", sender="15552220000")) is False


def test_window_expiry():
    """An identical message long afterwards is a new message."""
    d = CrossTransportDeduper(window_sec=10.0)
    assert d.is_duplicate(ev("ping", raw_type="iMessage"), now=1000.0) is False
    assert d.is_duplicate(ev("ping", raw_type="SMS_GSM"), now=1005.0) is True
    assert d.is_duplicate(ev("ping", raw_type="SMS_GSM"), now=1200.0) is False


def test_normalize_body():
    assert normalize_body(None) == ""
    assert normalize_body("  Hello   World  ") == "hello world"
    assert normalize_body("“curly”") == '"curly"'

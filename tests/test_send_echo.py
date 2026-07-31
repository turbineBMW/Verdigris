"""Messages you send must always appear, even if you send the same one twice.

The iPhone re-pushes messages you sent from its SENT folder under a different
handle, so handle-dedupe can't catch that copy and the UI matches on text
instead. Matching on text alone also ate genuine repeats: sending "test"
twice in three minutes showed one bubble, which looked exactly like sending
had stopped working.

The discriminator is where the event came from, not what it says.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from iphonebridge.qtui.models import ThreadStore


def thread(*msgs):
    return {"messages": list(msgs), "phone": "+12155550150"}


def sent(body, ts):
    return {"body": body, "ts": ts, "outgoing": True}


TS1 = "2026-07-30T10:00:00+00:00"
TS2 = "2026-07-30T10:00:30+00:00"   # 30s later — inside the echo window
TS3 = "2026-07-30T11:00:00+00:00"   # an hour later — outside it


def test_our_own_send_is_never_treated_as_an_echo():
    """The bug: the second identical send vanished."""
    t = thread(sent("test", TS1))
    assert ThreadStore._is_echo(t, "test", TS2, True, "sms_sent") is False


def test_the_phones_echo_of_our_send_is_still_suppressed():
    """The behaviour the check exists for, which must survive the fix."""
    t = thread(sent("on my way", TS1))
    assert ThreadStore._is_echo(t, "on my way", TS2, True, "SMS_GSM") is True


def test_apples_echo_to_our_other_devices_is_suppressed():
    """iMessage delivers our own sent messages back to us; still one message."""
    t = thread(sent("on my way", TS1))
    assert ThreadStore._is_echo(t, "on my way", TS2, True, "iMessage") is True


def test_an_echo_long_afterwards_is_a_real_message():
    t = thread(sent("ok", TS1))
    assert ThreadStore._is_echo(t, "ok", TS3, True, "SMS_GSM") is False


def test_incoming_messages_are_never_echoes():
    t = thread(sent("hello", TS1))
    assert ThreadStore._is_echo(t, "hello", TS2, False, "SMS_GSM") is False


def test_repeated_sends_all_survive():
    """Sending the same word five times must produce five bubbles."""
    t = thread()
    stamps = [f"2026-07-30T10:00:{s:02d}+00:00" for s in (0, 5, 10, 15, 20)]
    for ts in stamps:
        assert ThreadStore._is_echo(t, "ok", ts, True, "sms_sent") is False
        t["messages"].append(sent("ok", ts))
    assert len(t["messages"]) == 5

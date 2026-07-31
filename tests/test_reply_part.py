"""Apple's reply wire format encodes a range over the replied-to message.

rustpush builds `tg` as `r:<part>:<guid>`. Sending a bare `"0"` as the part
produced a `tg` Apple would not thread on, and it failed silently — the
message was delivered, just as an ordinary message rather than a reply.

The real format was read off replies sent by iOS itself, in the
`thread_originator_part` column of a device backup: always `0:0:<N>`, where N
is exactly the character count of the message being replied to.
"""
from __future__ import annotations

from iphonebridge.dbus_service import _reply_part


def test_part_encodes_the_targets_length():
    assert _reply_part("i think it was 20??") == "0:0:19"


def test_empty_target_is_a_zero_range():
    """No text to range over — an attachment-only target, or a caller that
    didn't supply it. Must still be well-formed, not a bare "0"."""
    assert _reply_part("") == "0:0:0"


def test_length_is_counted_in_utf16_code_units():
    """Apple counts NSString length, which is UTF-16 — an emoji outside the
    BMP is two units, not one. Counting Python characters would put the range
    end short and mis-thread any reply to a message containing emoji."""
    assert _reply_part("🎉") == "0:0:2"
    assert _reply_part("hi 🎉") == "0:0:5"


def test_bmp_characters_count_as_one():
    """A curly apostrophe is one UTF-16 unit, so it must not inflate the
    count — the backup's own rows confirm plain text counts as characters."""
    assert _reply_part("i don’t got luckys money") == "0:0:24"

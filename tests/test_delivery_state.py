"""Where delivery captions land.

The rule is Messages.app's, not "the last bubble": "Read" sits under the
newest message they've read and "Delivered" under the newest they haven't, so
both can be visible at once on different bubbles.

The case that motivated rewriting this: a "Read" must survive the other
person replying afterwards. An earlier version only painted a caption when
the thread's last message was ours, so the caption vanished as soon as they
answered — which is most of the time.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from iphonebridge.qtui.models import ThreadStore


def msgs(*specs):
    """Messages: (guid, outgoing, state) tuples, oldest first."""
    return [{"guid": g, "outgoing": o, "state": s} for g, o, s in specs]


def rows_for(messages):
    """One row per message, as _rows_for would produce for plain text."""
    return [{"guid": m["guid"], "outgoing": m["outgoing"], "deliveryState": ""}
            for m in messages]


def captions(messages):
    rows = rows_for(messages)
    ThreadStore._apply_delivery_captions(messages, rows)
    return {r["guid"]: r["deliveryState"] for r in rows if r["deliveryState"]}


def test_read_survives_their_reply():
    """The bug this file exists for."""
    m = msgs(("A", True, "read"), ("B", False, ""))
    assert captions(m) == {"A": "read"}


def test_read_and_delivered_can_show_together():
    """Newest read, plus a newer one they haven't read yet."""
    m = msgs(("A", True, "read"), ("B", True, "delivered"))
    assert captions(m) == {"A": "read", "B": "delivered"}


def test_only_the_newest_read_is_captioned():
    m = msgs(("A", True, "read"), ("B", True, "read"))
    assert captions(m) == {"B": "read"}


def test_a_later_read_supersedes_an_earlier_delivered():
    """Delivered before a read message is stale — it was read too."""
    m = msgs(("A", True, "delivered"), ("B", True, "read"))
    assert captions(m) == {"B": "read"}


def test_incoming_messages_never_get_a_caption():
    m = msgs(("A", False, "read"), ("B", False, "delivered"))
    assert captions(m) == {}


def test_no_receipts_means_no_caption():
    m = msgs(("A", True, ""), ("B", False, ""))
    assert captions(m) == {}


def test_a_moved_caption_is_cleared_from_the_old_bubble():
    """Recomputing must not leave a stale caption behind."""
    messages = msgs(("A", True, "read"), ("B", True, "delivered"))
    rows = rows_for(messages)
    ThreadStore._apply_delivery_captions(messages, rows)
    assert rows[0]["deliveryState"] == "read"

    # They read B as well: the caption should move off A entirely.
    messages[1]["state"] = "read"
    ThreadStore._apply_delivery_captions(messages, rows)
    assert rows[0]["deliveryState"] == ""
    assert rows[1]["deliveryState"] == "read"


def test_the_caption_goes_on_the_last_row_of_a_multi_row_message():
    """A photo plus caption is several rows but one message."""
    messages = msgs(("A", True, "read"))
    rows = [
        {"guid": "A", "outgoing": True, "deliveryState": "", "kind": "image"},
        {"guid": "A", "outgoing": True, "deliveryState": "", "kind": "text"},
    ]
    ThreadStore._apply_delivery_captions(messages, rows)
    assert rows[0]["deliveryState"] == ""
    assert rows[1]["deliveryState"] == "read"


def test_messages_without_a_guid_are_skipped():
    """MAP messages can't receive receipts, so they can't be captioned."""
    m = msgs(("", True, "read"),)
    assert captions(m) == {}

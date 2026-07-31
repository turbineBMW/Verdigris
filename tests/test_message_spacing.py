"""Consecutive messages minutes apart must not stack as tightly as a burst.

Three bubbles fired off in ten seconds are one thought; the same three spread
over an afternoon are not. Drawn with identical spacing there was no way to
tell them apart below the one-hour mark where the time divider takes over.
"""
from __future__ import annotations

from iphonebridge.qtui import models
from iphonebridge.qtui.models import ThreadStore


def _store() -> ThreadStore:
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._current = None
    s._by_guid = {}
    return s


def _msg(ts: str, body: str = "hi", **kw) -> dict:
    m = {"body": body, "ts": ts, "outgoing": False, "reaction": None,
         "attachments": [], "guid": "", "reply_to": "", "sender_name": "aiden",
         "sender_phone": "+12155550102", "edits": []}
    m.update(kw)
    return m


def test_burst_stays_tight():
    """Rapid-fire messages are one turn — extra space would break them up."""
    store = _store()
    prev = _msg("2026-07-30T12:00:00+00:00")
    rows = store._rows_for(_msg("2026-07-30T12:00:10+00:00"), prev)
    assert rows[0]["gapBefore"] == 0


def test_pause_gets_breathing_room():
    store = _store()
    prev = _msg("2026-07-30T12:00:00+00:00")
    rows = store._rows_for(_msg("2026-07-30T12:20:00+00:00"), prev)
    assert rows[0]["gapBefore"] == models._RUN_GAP_PX


def test_no_gap_when_a_divider_already_separates_them():
    """Past an hour the divider shows the break; adding a gap on top of it
    would double the separation."""
    store = _store()
    prev = _msg("2026-07-30T12:00:00+00:00")
    rows = store._rows_for(_msg("2026-07-30T18:00:00+00:00"), prev)
    assert rows[0]["divider"]
    assert rows[0]["gapBefore"] == 0


def test_first_message_has_no_gap():
    store = _store()
    rows = store._rows_for(_msg("2026-07-30T12:00:00+00:00"), None)
    assert rows[0]["gapBefore"] == 0


def test_gap_applies_across_speakers_too():
    """The pause is what's being shown, not who spoke — a reply twenty minutes
    later is just as separate as a second message twenty minutes later."""
    store = _store()
    prev = _msg("2026-07-30T12:00:00+00:00", outgoing=True, sender_name="")
    rows = store._rows_for(_msg("2026-07-30T12:20:00+00:00"), prev)
    assert rows[0]["gapBefore"] == models._RUN_GAP_PX


def test_gap_only_on_the_first_row_of_a_message():
    """A photo and its caption are one message and stay tight against each
    other however long the pause before them."""
    store = _store()
    prev = _msg("2026-07-30T12:00:00+00:00")
    msg = _msg("2026-07-30T12:20:00+00:00", body="look",
               attachments=[{"path": "/tmp/x.png", "mime": "image/png",
                             "name": "x.png"}])
    rows = store._rows_for(msg, prev)
    assert len(rows) == 2
    assert rows[0]["gapBefore"] == models._RUN_GAP_PX
    assert rows[1]["gapBefore"] == 0

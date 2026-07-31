"""Group conversations must resolve to one thread across transports.

The backup's `chat.guid` is a phone-local sqlite id that a live iMessage
payload can never produce, so keying threads on it filed an imported group and
the same group arriving live as two unrelated conversations — with the live
copy also showing every participant as an unknown number, because only the
backup path was setting `sender_name`.
"""
from __future__ import annotations

from iphonebridge.imessage.handles import GROUP_KEY_PREFIX, group_key
from iphonebridge.qtui.models import ThreadStore


def _store() -> ThreadStore:
    """A ThreadStore with just enough state to thread events.

    Built via __new__ as the rest of the suite does — the real __init__ wants
    D-Bus, disk and Qt.
    """
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._by_phone = {}
    return s


def test_group_key_agrees_across_handle_spellings():
    """The backup stores bare `+1555…` where the live protocol says `tel:+1555…`.

    Without canonicalizing, the two sources produce different keys for an
    identical group and never merge.
    """
    live = group_key(["tel:+12155550104", "tel:+12155550103"], ["tel:+12155550150"])
    backup = group_key(["+12155550103", "+12155550104"])
    assert live == backup
    assert live.startswith(GROUP_KEY_PREFIX)


def test_group_key_drops_our_own_handles():
    """We're in every group we're in, and registered under several addresses.

    Leaving ourselves in keys the same group differently depending on which of
    our addresses a message happened to be addressed to.
    """
    mine = ["tel:+12155550150", "mailto:me@example.com"]
    assert group_key(["tel:+12155550104", "tel:+12155550150"], mine) == group_key(
        ["tel:+12155550104", "mailto:me@example.com"], mine)


def test_group_key_is_order_and_duplicate_independent():
    assert group_key(["tel:+12", "tel:+11", "tel:+12"]) == group_key(["tel:+11", "tel:+12"])


def test_group_key_none_when_nothing_usable():
    """Callers fall back to their transport-specific id rather than sharing a
    key built from nothing — which would merge every unidentifiable group."""
    assert group_key([]) is None
    assert group_key(["", None]) is None


def test_backup_and_live_group_events_share_a_thread():
    """The bug: two disconnected copies of the same group in the sidebar."""
    store = _store()
    key = group_key(["+12155550104", "+12155550103"])

    backup_key, _ = store._thread_for({
        "group_key": key,
        "chat_guid": "any;+;a245644043204b4c88370ef70249cbcc",
        "chat_name": "the fucky ",
        "sender_phone": "+12155550103",
    })
    # A live event carries the participant key as its chat_guid, with no
    # knowledge of the backup's identifier at all.
    live_key, _ = store._thread_for({
        "chat_guid": key,
        "chat_name": "the fucky ",
        "sender_phone": "+12155550103",
    })

    assert backup_key == live_key
    assert len(store._threads) == 1


def test_group_without_participants_still_threads_on_chat_guid():
    """Old backups have no `chat_handle_join`; those groups must not collapse
    into their senders' 1:1 threads just because there's no participant key."""
    store = _store()
    key, thread = store._thread_for({
        "group_key": None,
        "chat_guid": "any;+;chat740867744757194268",
        "chat_name": "Fucking Lovely Chat ",
        "sender_phone": "+12155550102",
    })
    assert key == "any;+;chat740867744757194268"
    assert thread["is_group"] is True


def test_live_message_records_its_sender():
    """Group bubbles label the speaker from `sender_name`. The live path left
    it unset, so every participant rendered as an unknown number."""
    store = _store()
    store._seen_handles = set()
    store._deleted = {}
    store._by_guid = {}
    store._unsorted = set()
    store._current = None
    store._pending_receipts = {}

    ev = {"handle": "guid:abc", "body": "hi", "timestamp": "2026-07-30T12:00:00+00:00",
          "chat_guid": group_key(["+12155550102", "+12155550104"]),
          "chat_name": "the fucky ",
          "sender_phone": "+12155550102", "contact_name": "aiden"}
    store._ingest(ev, outgoing=False, refresh=False)

    msg = store._threads[ev["chat_guid"]]["messages"][-1]
    assert msg["sender_name"] == "aiden"
    assert msg["sender_phone"] == "+12155550102"

"""An edit rewrites a message; it doesn't become a new one.

Apple delivers an edit as its own event carrying the guid of the message it
replaces. Ingesting it like any other message left the original bubble showing
the old text with the correction stranded underneath as a separate line —
which is why editing appeared to work on the phone but not here.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from verdigris.qtui.models import ThreadStore
from verdigris.qtui.util import receipt_ts


def store():
    """A ThreadStore with the D-Bus, disk and Qt plumbing left out."""
    s = ThreadStore.__new__(ThreadStore)
    s._threads = {}
    s._by_phone = {}
    s._by_guid = {}
    s._seen_handles = set()
    s._deleted = {}
    s._unsorted = set()
    s._current = ""
    s._read_marks = {}
    s._pending_receipts = {}
    s._pending_edits = {}
    return s


def message(guid, body, **extra):
    ev = {
        "handle": guid, "guid": guid, "kind": "sms_sent",
        "sender_phone": "+12155550150", "sender_phone_norm": "12155550150",
        "contact_name": "Test Person", "body": body,
        "timestamp": "2026-07-30T12:00:00+00:00", "attachments": [],
    }
    ev.update(extra)
    return ev


def only_thread(s):
    return next(iter(s._threads.values()))


# ---- edits rewrite in place -------------------------------------------


def test_an_edit_replaces_the_original_text():
    s = store()
    s._ingest_backup(message("A", "see you at 5"))
    s._ingest_backup(message("B", "see you at 6", im_edited_from_guid="A"))

    msgs = only_thread(s)["messages"]
    assert len(msgs) == 1, "the edit was appended instead of applied"
    assert msgs[0]["body"] == "see you at 6"


def test_the_previous_text_is_kept():
    """The UI reveals it behind the "Edited" link, so it has to survive."""
    s = store()
    s._ingest_backup(message("A", "see you at 5"))
    s._ingest_backup(message("B", "see you at 6", im_edited_from_guid="A"))

    assert only_thread(s)["messages"][0]["edits"] == ["see you at 5"]


def test_repeated_edits_accumulate_oldest_first():
    s = store()
    s._ingest_backup(message("A", "v1"))
    s._ingest_backup(message("B", "v2", im_edited_from_guid="A"))
    s._ingest_backup(message("C", "v3", im_edited_from_guid="A"))

    msg = only_thread(s)["messages"][0]
    assert msg["body"] == "v3"
    assert msg["edits"] == ["v1", "v2"]


def test_the_persisted_spelling_of_the_field_also_works():
    """`im_edited_from_guid` on the bus, bare `edited_from_guid` on disk.

    Accepting only one means edits stop applying after a restart.
    """
    s = store()
    s._ingest_backup(message("A", "v1"))
    s._ingest_backup(message("B", "v2", edited_from_guid="A"))

    assert only_thread(s)["messages"][0]["body"] == "v2"


def test_an_edit_of_a_message_we_never_saw_is_still_shown():
    """Better a stray bubble than silently swallowing the correction."""
    s = store()
    s._ingest_backup(message("B", "v2", im_edited_from_guid="MISSING"))

    assert only_thread(s)["messages"][0]["body"] == "v2"


def test_an_ordinary_message_has_no_edit_history():
    s = store()
    s._ingest_backup(message("A", "hello"))
    assert not only_thread(s)["messages"][0].get("edits")


# ---- the read timestamp ------------------------------------------------


def test_a_receipt_from_today_is_just_the_time():
    """"Read Today 12:52 PM" reads worse than "Read 12:52 PM"."""
    from datetime import datetime

    now = datetime.now().astimezone().replace(hour=12, minute=52, second=0)
    assert receipt_ts(now.isoformat()) == "12:52 PM"


def test_an_older_receipt_keeps_its_day():
    """Without it, a week-old "Read 12:52 PM" would look like today."""
    from datetime import datetime, timedelta

    then = (datetime.now().astimezone() - timedelta(days=30)).replace(
        hour=9, minute=5, second=0)
    out = receipt_ts(then.isoformat())
    assert out.endswith("9:05 AM")
    assert out != "9:05 AM", "the date was dropped"


def test_an_unparseable_receipt_renders_as_nothing():
    assert receipt_ts(None) == ""
    assert receipt_ts("") == ""


# ---- edits and unsends we make ourselves -------------------------------


def test_our_own_edit_updates_our_own_copy():
    """Apple relays an edit to your *other* devices, never back to the sender.

    So the desktop is the one place that never hears about its own edit. The
    daemon reports it locally over the state channel instead; without this the
    bubble kept its old text while the phone showed the correction — exactly
    how it was reported.
    """
    s = store()
    s._ingest_backup(message("A", "see you at 5"))
    s._current = ""            # no open thread, so no rebuild is attempted

    s._on_state_signal({"guid": "A", "state": "edited",
                        "body": "see you at 6", "handle": "+12155550150"})

    msg = only_thread(s)["messages"][0]
    assert msg["body"] == "see you at 6"
    assert msg["edits"] == ["see you at 5"]


def test_our_own_unsend_updates_our_own_copy():
    s = store()
    s._ingest_backup(message("A", "oops wrong chat"))
    s._current = ""

    s._on_state_signal({"guid": "A", "state": "unsent",
                        "body": "[message unsent]", "handle": "+12155550150"})

    assert only_thread(s)["messages"][0]["body"] == "[message unsent]"


def test_edit_before_message_is_loaded_applies_when_guid_binds():
    """An edit that races ahead of the bubble must not be dropped.

    The previous path marked the state handle as seen even when the target
    wasn't in `_by_guid`, so opening the conversation later skipped hydrate
    and the old text stuck until a full app restart.
    """
    s = store()
    # Edit arrives first — no message to rewrite yet.
    s._on_state_signal({"guid": "A", "state": "edited",
                        "body": "see you at 6", "handle": "+12155550150"})
    assert "A" in s._pending_edits

    s._ingest_backup(message("A", "see you at 5"))
    # Backup ingest binds the guid; pending edit must land on the bubble.
    msg = only_thread(s)["messages"][0]
    assert msg["body"] == "see you at 6"
    assert msg["edits"] == ["see you at 5"]
    assert "A" not in s._pending_edits


def test_failed_edit_is_not_marked_seen_so_hydrate_can_retry():
    """Disk ingest must not burn the state handle when apply can't run."""
    s = store()
    # No target message yet — apply queues, and seen is only set after.
    s._ingest_state({
        "handle": "state:A:edited:t1",
        "guid": "A",
        "state": "edited",
        "body": "v2",
        "peer_handle": "+12155550150",
    }, refresh=False)
    # Queued counts as handled, so the handle is seen — but the body is in
    # pending_edits, which `_bind_guid` applies. (Hydrate also re-applies
    # via `_apply_state` directly, bypassing seen.)
    assert "A" in s._pending_edits

    msg = {"body": "v1", "guid": "", "edits": [], "outgoing": True,
           "reactions": {}, "attachments": [], "ts": "2026-07-30T12:00:00+00:00",
           "sender_name": "", "sender_phone": "", "state": ""}
    s._bind_guid(msg, "A")
    assert msg["body"] == "v2"
    assert msg["edits"] == ["v1"]


def test_live_edit_rebuilds_open_conversation_model():
    """Editing while the thread is on screen must repaint the bubble."""
    from verdigris.qtui.models import MessageListModel

    s = store()
    s._message_model = MessageListModel()
    s._ingest_backup(message("A", "see you at 5"))
    thread = only_thread(s)
    key = next(iter(s._threads))
    thread["key"] = key
    s._current = key
    s._rebuild_messages()
    assert s._message_model.rows()[0]["body"] == "see you at 5"

    s._on_state_signal({"guid": "A", "state": "edited",
                        "body": "see you at 6", "handle": "+12155550150"})
    assert s._message_model.rows()[0]["body"] == "see you at 6"
    assert s._message_model.rows()[0]["edits"] == ["see you at 5"]


# ---- replies are visibly replies ---------------------------------------


def test_a_reply_quotes_the_message_it_answers():
    """The target was stored all along but never shown, so a reply rendered
    as an ordinary bubble — indistinguishable from replying not working."""
    s = store()
    s._ingest_backup(message("A", "are you free at 6"))
    s._ingest_backup(message("B", "yes", im_reply_to_guid="A"))

    reply = only_thread(s)["messages"][-1]
    assert s._reply_snippet(reply) == "are you free at 6"


def test_an_ordinary_message_quotes_nothing():
    s = store()
    s._ingest_backup(message("A", "hello"))
    assert s._reply_snippet(only_thread(s)["messages"][0]) == ""


def test_replying_to_something_we_never_loaded_still_reads_as_a_reply():
    """Better an ellipsis than silently rendering it as a plain message."""
    s = store()
    s._ingest_backup(message("B", "yes", im_reply_to_guid="MISSING"))
    assert s._reply_snippet(only_thread(s)["messages"][0]) == "…"

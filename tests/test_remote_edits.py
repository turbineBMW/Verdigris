"""Edits, unsends and replies made by *other* people must reach the UI.

`_echo_local_change` covered edits we make, because Apple never relays those
back to the sender. The other direction had no handler at all: a remote edit
was fanned out as an ordinary message, so the original kept its old text and
the correction appeared as a separate bubble — and since Apple sends no
participant list on an Edit, that bubble had no chat_guid and filed itself in
the sender's 1:1 thread instead of the group.

Nothing wrote it to disk either. Only `message_state` rows carry an edit
across a restart, so even an edit that applied correctly live was lost on the
next reload.
"""
from __future__ import annotations

from iphonebridge.imessage.bridge import translate

MY = ["tel:+12155550150"]
GROUP = ["tel:+12155550150", "tel:+12155550102", "tel:+12155550103"]


def _event(message: dict, sender: str = "tel:+12155550102",
           participants: list[str] | None = None) -> dict:
    return {"event": "message", "inst": {
        "id": "6E63E81B", "sender": sender,
        "conversation": {"participants": participants
                         if participants is not None else GROUP,
                         "cv_name": "the fucky "},
        "message": message,
    }}


class _Recorder:
    """Stands in for the D-Bus service, capturing what the daemon emits."""

    def __init__(self):
        self.states = []
        self.messages = []

    def emit_message_state(self, guid, state, handle="", timestamp="", body=""):
        self.states.append({"guid": guid, "state": state, "handle": handle,
                            "body": body})

    def emit_message(self, event, extras=None):
        self.messages.append(event)


def _daemon(rec):
    """A Daemon with only the fields _emit_remote_edit touches."""
    from iphonebridge.daemon import Daemon
    d = Daemon.__new__(Daemon)
    d._dbus_service = rec
    d.sinks = []
    return d


def test_remote_edit_goes_out_as_state_not_a_message():
    rec = _Recorder()
    tr = translate(_event({"Edit": {
        "tuuid": "608E238B",
        "new_parts": [{"part": {"Text": ["we got heads tn", {}]}}],
    }}), MY)
    assert _daemon(rec)._emit_remote_edit(tr) is True

    assert len(rec.states) == 1
    st = rec.states[0]
    # The *target*, not the edit event's own id — that's what the UI looks a
    # message up by.
    assert st["guid"] == "608E238B"
    assert st["state"] == "edited"
    assert st["body"] == "we got heads tn"


def test_remote_unsend_uses_the_same_placeholder_as_our_own():
    rec = _Recorder()
    tr = translate(_event({"Unsend": {"tuuid": "608E238B"}}), MY)
    assert _daemon(rec)._emit_remote_edit(tr) is True
    assert rec.states[0]["state"] == "unsent"
    assert rec.states[0]["body"] == "[message unsent]"


def test_ordinary_message_is_not_treated_as_an_edit():
    """Otherwise every message would vanish into the state channel."""
    rec = _Recorder()
    tr = translate(_event({"Message": {
        "parts": [{"part": {"Text": ["we got heads tm", {}]}}]}}), MY)
    assert _daemon(rec)._emit_remote_edit(tr) is False
    assert rec.states == []


def test_incoming_reply_target_persists_on_the_event():
    """`reply_to_guid` has to be on the SmsEvent, not just the live-only
    extras — only event fields reach events.jsonl, so a reply left on extras
    reloaded as an ordinary bubble after a restart."""
    tr = translate(_event({"Message": {
        "parts": [{"part": {"Text": ["i don’t got luckys money", {}]}}],
        "reply_guid": "CHRIS-GUID",
    }}), MY)
    assert tr.extras.reply_to_guid == "CHRIS-GUID"
    assert tr.event.reply_to_guid == "CHRIS-GUID"
    assert tr.event.to_dict()["reply_to_guid"] == "CHRIS-GUID"


def test_non_reply_leaves_the_field_empty():
    tr = translate(_event({"Message": {
        "parts": [{"part": {"Text": ["hi", {}]}}]}}), MY)
    assert tr.event.reply_to_guid is None

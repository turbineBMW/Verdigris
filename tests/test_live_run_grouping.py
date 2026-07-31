"""A message arriving while the thread is open must close the previous run.

The tail and the group avatar mark the *end* of a run, so only the message
that follows can decide where one ends. The incremental append path drew each
arriving bubble as if it were last and never revisited the one before it, so
while a thread stayed open every consecutive bubble kept its tail and, in a
group, the sender's avatar repeated down the whole run. Reopening the thread
hid it, because that path rebuilds every row knowing what follows.
"""
from __future__ import annotations

from iphonebridge.qtui.models import MessageListModel, ThreadStore


def _msg(ts: str, sender: str = "aiden", outgoing: bool = False,
         body: str = "hi") -> dict:
    return {"body": body, "ts": ts, "outgoing": outgoing, "reaction": None,
            "attachments": [], "guid": "", "reply_to": "",
            "sender_name": sender, "sender_phone": "+12155550102",
            "edits": []}


def test_run_continues_for_the_same_speaker():
    assert ThreadStore._continues_run(
        _msg("2026-07-30T12:00:00+00:00"),
        _msg("2026-07-30T12:00:10+00:00")) is True


def test_run_breaks_between_speakers():
    assert ThreadStore._continues_run(
        _msg("2026-07-30T12:00:00+00:00", sender="aiden"),
        _msg("2026-07-30T12:00:10+00:00", sender="mari")) is False


def test_run_breaks_across_direction():
    assert ThreadStore._continues_run(
        _msg("2026-07-30T12:00:00+00:00", outgoing=False),
        _msg("2026-07-30T12:00:10+00:00", outgoing=True)) is False


def test_a_time_divider_ends_the_run():
    """Otherwise a burst separated by hours is re-labelled but keeps its
    avatar stranded at the far end."""
    assert ThreadStore._continues_run(
        _msg("2026-07-30T12:00:00+00:00"),
        _msg("2026-07-30T18:00:00+00:00")) is False


def test_nothing_before_it_is_not_a_run():
    assert ThreadStore._continues_run(None, _msg("2026-07-30T12:00:00+00:00")) is False


def test_close_run_strips_tail_and_avatar():
    model = MessageListModel()
    model.append({"tail": True, "senderAvatar": "file:///a.png",
                  "senderInitials": "A"})
    model.close_run()
    row = model.rows()[-1]
    assert row["tail"] is False
    assert row["senderAvatar"] == ""
    assert row["senderInitials"] == ""


def test_close_run_is_safe_on_an_empty_model():
    model = MessageListModel()
    model.close_run()
    assert model.rows() == []

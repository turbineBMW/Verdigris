"""A dead Apple connection has to look dead to the daemon.

Apple resets the APNs socket periodically. The helper's unix socket survives
that, so every liveness signal the daemon had kept saying "available" while
nothing inbound arrived — messages sent from the iPhone stopped appearing for
hours, with nothing in the log to explain it. The helper now reports the loss
(and then exits so systemd rebuilds the connection); this is the daemon half.
"""
from __future__ import annotations

from verdigris.daemon import Daemon


def _daemon():
    d = Daemon.__new__(Daemon)
    d._imessage = object()
    d._dbus_service = None
    d._imessage_retry_id = None
    return d


def test_a_dead_apple_connection_drops_the_transport():
    d = _daemon()
    d._on_imessage_event({"event": "disconnected"})
    assert d._imessage is None
    assert d._imessage_retry_id is not None


def test_a_dead_socket_still_drops_the_transport():
    """The pre-existing signal must keep working — this is the path that
    fires when the helper process goes away entirely."""
    d = _daemon()
    d._on_imessage_event({"event": "helper_disconnected"})
    assert d._imessage is None
    assert d._imessage_retry_id is not None


def test_one_retry_timer_however_many_losses():
    """Both signals arrive for the same outage — the helper announces the
    lost connection and then exits, closing the socket a moment later. A
    second timer would double the reconnect rate for good."""
    d = _daemon()
    d._on_imessage_event({"event": "disconnected"})
    first = d._imessage_retry_id
    d._on_imessage_event({"event": "helper_disconnected"})
    assert d._imessage_retry_id == first


def test_ordinary_events_are_left_alone():
    """The guard is on the event name, and message events carry none — a
    sloppy test here would swallow every message instead."""
    d = _daemon()
    d._imessage_handles = []
    d._on_imessage_event({"event": "message", "inst": None})
    assert d._imessage is not None
    assert d._imessage_retry_id is None

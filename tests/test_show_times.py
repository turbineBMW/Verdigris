"""Every message row has to carry its own time of day.

Show Times reveals a stamp beside *both* sides of the conversation, and the
delegate can only draw what the row carries — `deliveryStamp` was no use for
it, being set on the newest outgoing bubble alone.
"""
from __future__ import annotations

from verdigris.qtui.models import MessageListModel
from verdigris.qtui.util import clock_ts


def test_the_role_is_exported_to_qml():
    """A role the delegate declares `required` and the model does not export
    stops the whole list rendering, not just the stamp."""
    assert "timeStamp" in MessageListModel._ROLES.values()


def test_the_role_number_is_unique():
    values = list(MessageListModel._ROLES)
    assert len(values) == len(set(values))


def test_time_only_whatever_the_date():
    """Dated stamps in the gutter would repeat what the conversation's date
    dividers already say, on every single line."""
    assert clock_ts("2026-03-04T18:24:00-05:00") == "6:24 PM"
    assert clock_ts("2019-01-02T06:05:00-05:00") == "6:05 AM"


def test_midnight_and_noon_read_as_12():
    """%-I, not %-H: a 0:24 AM in the gutter is a bug report waiting to
    happen."""
    assert clock_ts("2026-03-04T00:24:00-05:00") == "12:24 AM"
    assert clock_ts("2026-03-04T12:00:00-05:00") == "12:00 PM"


def test_a_message_with_no_timestamp_shows_nothing():
    """Backup rows and MAP messages can both arrive without one, and "" is
    what leaves the gutter blank rather than drawing junk."""
    for value in (None, "", "not a date"):
        assert clock_ts(value) == ""

"""Opening a conversation has to take its notifications off the screen.

The sink's only automatic close is a BlueZ `Message1.Read` property change,
which is a MAP object. An iMessage arrives with no such object to watch, so on
the transport that now carries most messages nothing ever closed a popup and
they accumulated until dismissed by hand.

The two halves are tested apart from D-Bus: which handles the UI sends, and
whether the sink can match them to the popups it opened.

Also: while a thread is open *and* the window is focused, new messages for
that conversation must not raise a popup (they still do when unfocused).

Message popups also auto-expire after NOTIFICATION_TIMEOUT_SEC; that path
must not mark-read on the iPhone.
"""
from __future__ import annotations

from verdigris.events import SmsEvent
from verdigris.imessage.handles import GROUP_KEY_PREFIX, group_key
from verdigris.qtui.models import ThreadStore
from verdigris.sinks import libnotify as libnotify_mod
from verdigris.sinks.libnotify import LibnotifySink

_key = LibnotifySink._peer_key


def test_the_same_person_keys_the_same_from_either_side():
    """The UI knows a sender by what the message carried, the popup by what
    the transport gave us, and for one person those differ."""
    assert _key("tel:+12155550101") == _key("+1 (215) 555-0101")
    assert _key("2155550101") == _key("+12155550101")


def test_email_handles_survive():
    """normalize_phone would strip an iMessage address to nothing, and every
    such popup would then key to None and never close."""
    assert _key("Someone@Example.com") == "someone@example.com"


def test_blank_handles_key_to_nothing():
    for value in ("", "   ", None):
        assert _key(value) is None


def _store(messages: list[dict], phone: str | None = "+12155550101"):
    store = ThreadStore.__new__(ThreadStore)
    store._threads = {}
    store._read_marks = {}
    store._save_read_marks = lambda: None
    store._window_focused = True
    store._current = None
    sent: list[str] = []
    active: list[tuple[str, bool]] = []
    store._client = type(
        "C",
        (),
        {
            "dismiss_notifications": lambda self, p: sent.append(p),
            "set_active_thread": lambda self, p, f: active.append((p, f)),
        },
    )()
    thread = {"key": "k", "messages": messages, "phone": phone, "unread": 3}
    return store, thread, sent, active


def _msg(sender: str | None, outgoing: bool = False) -> dict:
    return {"sender_phone": sender, "outgoing": outgoing, "ts": "2026-07-31"}


def test_reading_a_thread_dismisses_its_popups():
    store, thread, sent, _ = _store([_msg("+12155550101")])
    store._mark_read(thread)
    assert sent and "+12155550101" in sent[0]


def test_a_group_clears_every_member_who_spoke():
    """A group notifies under whoever sent the message, so dismissing only the
    thread's own handle leaves the rest of the popups on screen."""
    store, thread, sent, _ = _store(
        [_msg("+12155550001"), _msg("+12155550002"), _msg("+12155550003")],
        phone=None,
    )
    store._mark_read(thread)
    for handle in ("+12155550001", "+12155550002", "+12155550003"):
        assert handle in sent[0]


def test_outgoing_messages_contribute_no_handles():
    """We never raise a popup for our own message, so our own number must not
    reach the dismiss call — it would be asking to close the other party's
    popups under our handle. The thread's own peer is used instead."""
    store, thread, sent, _ = _store([_msg("+12155550150", outgoing=True)])
    store._mark_read(thread)
    assert sent == ["+12155550101"]


def test_the_handle_list_is_bounded():
    """A long thread would otherwise put hundreds of handles on the wire every
    time you switched to it."""
    store, thread, sent, _ = _store(
        [_msg(f"+1215555{n:04d}") for n in range(500)], phone=None)
    store._mark_read(thread)
    assert len(sent[0].split(",")) <= 16


def test_a_thread_with_no_incoming_handles_falls_back_to_its_own():
    store, thread, sent, _ = _store([_msg(None)])
    store._mark_read(thread)
    assert sent == ["+12155550101"]


def test_a_failing_daemon_does_not_break_opening_a_thread():
    """Dismissal is cosmetic; a daemon that isn't there must not stop the UI
    from marking the thread read."""
    store, thread, _, _ = _store([_msg("+12155550101")])

    def boom(_self, _peers):
        raise RuntimeError("daemon not reachable")

    store._client = type(
        "C",
        (),
        {
            "dismiss_notifications": boom,
            "set_active_thread": lambda self, p, f: None,
        },
    )()
    store._mark_read(thread)
    assert thread["unread"] == 0


# ---- suppress when open + focused ---------------------------------------


def _sink_without_bus() -> LibnotifySink:
    """LibnotifySink without touching D-Bus (only pure logic exercised)."""
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._hfp = None
    sink._send_message = None
    sink._contacts = None
    sink._reply_targets = {}
    sink._peer_notifs = {}
    sink._active_peers = set()
    sink._active_chat_keys = set()
    sink._app_focused = False
    sink._pending = {}
    sink._msg_subs = {}
    sink._call_notifs = {}
    sink._notif_calls = {}
    return sink


def _sms(
    phone: str = "+12155550101",
    *,
    chat_guid: str | None = None,
    guid: str | None = None,
    body: str = "hello",
    kind: str = "sms_received",
) -> SmsEvent:
    return SmsEvent(
        kind=kind,  # type: ignore[arg-type]
        handle=guid or "h1",
        sender_phone=phone,
        sender_phone_norm="".join(c for c in phone if c.isdigit()) or None,
        contact_name=None,
        body=body,
        timestamp=None,
        is_read=False,
        raw_status=None,
        raw_type="iMessage",
        chat_guid=chat_guid,
        guid=guid,
    )


def test_focused_open_one_to_one_suppresses_popup():
    sink = _sink_without_bus()
    sink.set_active_thread("+12155550101", True)
    assert sink._should_suppress(_sms("+1 (215) 555-0101"))


def test_unfocused_open_thread_still_notifies():
    """Minimized / other workspace / covered — still raise the popup."""
    sink = _sink_without_bus()
    sink.set_active_thread("+12155550101", False)
    assert not sink._should_suppress(_sms("+12155550101"))


def test_focused_but_other_thread_still_notifies():
    sink = _sink_without_bus()
    sink.set_active_thread("+12155550101", True)
    assert not sink._should_suppress(_sms("+12155550999"))


def test_clearing_active_thread_restores_notifications():
    sink = _sink_without_bus()
    sink.set_active_thread("+12155550101", True)
    sink.set_active_thread("", True)
    assert not sink._should_suppress(_sms("+12155550101"))


def test_group_key_with_embedded_commas_is_not_split():
    """imessage-group: keys list participants with commas; splitting them
    would destroy the identity and never match chat_guid on the event."""
    gkey = group_key(["tel:+12155550001", "tel:+12155550002"])
    assert gkey and gkey.startswith(GROUP_KEY_PREFIX)
    sink = _sink_without_bus()
    sink.set_active_thread(gkey, True)
    assert gkey in sink._active_chat_keys
    assert sink._should_suppress(
        _sms("+12155550001", chat_guid=gkey, guid="G-1"))


def test_group_also_matches_via_speaker_peer_key():
    """Expanding group participants lets a MAP-less path that only has the
    sender still suppress when that speaker is in the open group."""
    gkey = group_key(["tel:+12155550001", "tel:+12155550002"])
    sink = _sink_without_bus()
    sink.set_active_thread(gkey, True)
    # Event without chat_guid (e.g. MAP) but from a known group member.
    assert sink._should_suppress(_sms("+12155550001"))


def test_ui_pushes_group_key_not_comma_joined_phones():
    store, _, _, active = _store([], phone=None)
    gkey = group_key(["tel:+12155550001", "tel:+12155550002"])
    thread = {"key": gkey, "messages": [], "phone": None, "is_group": True}
    store._threads[gkey] = thread
    store._current = gkey
    store._window_focused = True
    store._push_active_thread()
    assert active == [(gkey, True)]


def test_ui_clears_active_when_window_unfocused():
    store, _, _, active = _store([_msg("+12155550101")])
    thread = {"key": "k", "messages": [], "phone": "+12155550101"}
    store._threads["k"] = thread
    store._current = "k"
    store._window_focused = True
    store._push_active_thread()
    assert active[-1] == ("+12155550101", True)
    store.setWindowFocused(False)
    assert active[-1] == ("", False)


# ---- group inline reply targets the group + that sender's message -------


def test_one_to_one_reply_context_is_the_sender():
    ctx = LibnotifySink._reply_context(
        _sms("+12155550101", guid="MSG-1", body="hey"))
    assert ctx is not None
    assert ctx["recipient"] == "+12155550101"
    assert ctx["reply_to_guid"] == "MSG-1"
    assert ctx["target_text"] == "hey"


def test_group_reply_context_posts_to_the_group_threaded_to_sender():
    """Inline reply on a group popup must not DM the speaker — it must land
    in the group as a reply to their message."""
    gkey = group_key(["tel:+12155550001", "tel:+12155550002"])
    assert gkey
    ctx = LibnotifySink._reply_context(
        _sms("+12155550001", chat_guid=gkey, guid="MSG-G", body="who is free?"))
    assert ctx is not None
    # Recipient is the participant set (without us), not only the speaker.
    parts = set(ctx["recipient"].split(","))
    assert "tel:+12155550001" in parts
    assert "tel:+12155550002" in parts
    assert ctx["reply_to_guid"] == "MSG-G"
    assert ctx["sender"] == "+12155550001"
    assert "who is free" in ctx["target_text"]


def test_inline_reply_callback_gets_reply_to_guid():
    """_on_reply must pass the stored reply_to_guid through to send_message."""
    sink = _sink_without_bus()
    calls: list[tuple] = []
    sink._send_message = lambda *a: calls.append(a)
    sink._reply_targets[7] = {
        "recipient": "tel:+12155550001,tel:+12155550002",
        "sender": "+12155550001",
        "reply_to_guid": "MSG-G",
        "target_text": "who is free?",
    }
    sink._on_reply(7, "I am")
    assert calls == [
        ("tel:+12155550001,tel:+12155550002", "I am", "MSG-G", "who is free?")
    ]


# ---- auto-expire timeout -------------------------------------------------


def test_expire_timeout_defaults_to_fifteen_seconds(monkeypatch):
    monkeypatch.setattr(libnotify_mod, "NOTIFICATION_TIMEOUT_SEC", 15)
    assert libnotify_mod._expire_timeout_ms() == 15_000


def test_expire_timeout_zero_means_never(monkeypatch):
    monkeypatch.setattr(libnotify_mod, "NOTIFICATION_TIMEOUT_SEC", 0)
    assert libnotify_mod._expire_timeout_ms() == 0


def test_message_notify_uses_configured_timeout(monkeypatch):
    """Notify's expire_timeout arg must reflect NOTIFICATION_TIMEOUT_SEC."""
    monkeypatch.setattr(libnotify_mod, "NOTIFICATION_TIMEOUT_SEC", 8)
    sink = _sink_without_bus()
    captured: list[int] = []

    def fake_notify(*args):
        # Signature: app, replaces_id, icon, title, body, actions, hints, timeout
        captured.append(int(args[7]))
        return 42

    sink._notif = type("N", (), {"Notify": staticmethod(fake_notify)})()
    sink.handle(_sms("+12155550101", body="ping"))
    assert captured == [8_000]


def test_expired_popup_does_not_mark_read_on_iphone():
    """Reason 1 (expired) must leave the message unread.

    Path is present so a bug that treated expire as dismiss would try to
    write Read=true over D-Bus. The early return on non-dismiss reasons
    keeps us from reaching that path at all.
    """
    sink = _sink_without_bus()
    sink._pending[9] = "/org/bluez/obex/message0"
    sink._on_closed(9, 1)  # reason 1 = expired
    assert 9 not in sink._pending

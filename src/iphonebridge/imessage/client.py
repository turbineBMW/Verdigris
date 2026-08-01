"""Talking to the `ib-imessage` helper over its unix socket.

The helper owns the Apple connection; this is a thin, synchronous client for
it. Requests carry an `id` and get exactly one reply with the same `id`;
anything arriving without one is an unsolicited event and goes to the callback.

Threading: one background reader thread owns the socket's read side, because
replies and events are interleaved on the same stream and only the reader can
tell them apart. Callers block on a per-request `Event`, so `send()` from the
UI thread behaves like an ordinary function call.

Deliberately not asyncio: the daemon is GLib-mainloop based (see `bus`), and
mixing an event loop in would mean either a second loop or converting the
daemon. A reader thread plus queues is the smaller change.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Imported only to recognise dbus-python's boolean, which needs coercing (see
# `_plain`). Optional on purpose: this module is usable without a bus, and the
# Qt UI's client talks QtDBus rather than dbus-python.
try:
    from dbus import Boolean as _DBusBoolean
except Exception:  # pragma: no cover - dbus-python not installed
    _DBusBoolean = None

# The helper does its own APNs reconnection, but a send still has to wait for
# per-recipient IDS lookups, which can be slow on a cold key cache.
DEFAULT_TIMEOUT = 45.0

# Where the helper listens by default. Under $XDG_RUNTIME_DIR so it's
# per-user, tmpfs-backed and cleaned up on logout.
def default_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    # The subdirectory is systemd's RuntimeDirectory=iphonebridge. The socket
    # can't sit directly in $XDG_RUNTIME_DIR because the unit runs under
    # ProtectSystem=strict, which mounts /run read-only.
    return os.path.join(runtime, "iphonebridge", "imessage.sock")


class IMessageError(RuntimeError):
    """The helper rejected a request, or Apple did."""


class IMessageUnavailable(IMessageError):
    """The helper isn't running or the socket went away.

    Distinct from `IMessageError` because it's the expected state whenever the
    helper is stopped — for instance while OpenBubbles is open to renew the
    registration, since the two cannot both hold the APNs token.
    """


def _plain(value):
    """Reduce dbus-python wrapper types to exact Python builtins.

    D-Bus methods often forward their arguments straight through, and
    dbus-python's types are subclasses: `dbus.String` of `str`, `dbus.Int32`
    of `int`. Most survive `json.dumps` unchanged, but `dbus.Boolean`
    subclasses `int`, so it serializes as `1` rather than `true` and the
    helper rejects it — Rust distinguishes the two.

    A `default=` hook cannot fix this, because json *can* serialize those
    types; it just serializes them wrongly. So the coercion has to happen
    before dumps, not as a fallback inside it.

    Note that `isinstance(dbus.Boolean(True), bool)` is False — it derives
    from `int` — so a plain bool-first check does not catch it and the type
    has to be named explicitly.
    """
    if _DBusBoolean is not None and isinstance(value, _DBusBoolean):
        return bool(value)
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    reply: dict | None = None


class IMessageClient:
    """Synchronous client for the `ib-imessage` helper.

    `on_event` is called from the reader thread for every unsolicited event,
    so it must not block: hand work to the UI's own loop (`GLib.idle_add`) or
    a queue.
    """

    def __init__(
        self,
        socket_path: str | None = None,
        on_event: Callable[[dict], None] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.socket_path = socket_path or default_socket_path()
        self.on_event = on_event
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[str, _Pending] = {}
        # Guards _pending and _sock. Sends happen from arbitrary threads.
        self._lock = threading.Lock()
        self._closing = False

    # -- connection ------------------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.socket_path)
        except OSError as e:
            raise IMessageUnavailable(
                f"iMessage helper not reachable at {self.socket_path}: {e}"
            ) from e
        self._sock = sock
        self._closing = False
        self._reader = threading.Thread(
            target=self._read_loop, name="imessage-reader", daemon=True
        )
        self._reader.start()

    def close(self) -> None:
        self._closing = True
        with self._lock:
            sock, self._sock = self._sock, None
            # Wake anyone still waiting rather than letting them time out.
            for pending in self._pending.values():
                pending.reply = {"ok": False, "error": "client closed"}
                pending.event.set()
            self._pending.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _read_loop(self) -> None:
        buf = b""
        sock = self._sock
        assert sock is not None
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._dispatch(line)
        except OSError as e:
            if not self._closing:
                log.warning("iMessage helper connection lost: %s", e)
        finally:
            if not self._closing:
                # Fail waiters explicitly; otherwise every in-flight call
                # blocks for the full timeout for no reason.
                with self._lock:
                    for pending in self._pending.values():
                        pending.reply = {
                            "ok": False,
                            "error": "helper disconnected",
                        }
                        pending.event.set()
                    self._pending.clear()
                    self._sock = None
                if self.on_event:
                    self.on_event({"event": "helper_disconnected"})

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("helper sent a non-JSON line: %r", line[:200])
            return
        if not isinstance(msg, dict):
            return

        req_id = msg.get("id")
        if req_id is not None:
            with self._lock:
                pending = self._pending.pop(str(req_id), None)
            if pending is not None:
                pending.reply = msg
                pending.event.set()
                return
            # A reply whose waiter already gave up. Nothing to do, but worth
            # knowing about when diagnosing timeouts.
            log.debug("late reply for request %s", req_id)
            return

        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                # A raising callback must not kill the reader thread — that
                # would silently stop all further events.
                log.exception("iMessage event handler raised")

    # -- requests --------------------------------------------------------

    def request(self, cmd: str, *, _timeout: float | None = None, **params) -> dict:
        """Send one command and wait for its reply.

        Returns the reply's payload. Raises `IMessageError` if the helper
        reported a failure.

        `_timeout` overrides the client's default for this call alone —
        underscored so it can't collide with a command parameter. Downloading
        a video is not a 45-second operation on a slow link, and raising the
        shared default would make every genuinely-stuck send hang that much
        longer before reporting.
        """
        if self._sock is None:
            self.connect()
        req_id = uuid.uuid4().hex[:12]
        payload = _plain({"id": req_id, "cmd": cmd, **params})
        line = (json.dumps(payload) + "\n").encode()

        pending = _Pending()
        with self._lock:
            self._pending[req_id] = pending
            sock = self._sock
        if sock is None:
            raise IMessageUnavailable("iMessage helper is not connected")
        try:
            sock.sendall(line)
        except OSError as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise IMessageUnavailable(f"could not write to helper: {e}") from e

        wait = self.timeout if _timeout is None else _timeout
        if not pending.event.wait(wait):
            with self._lock:
                self._pending.pop(req_id, None)
            raise IMessageError(f"{cmd} timed out after {wait:g}s")

        reply = pending.reply or {}
        if not reply.get("ok"):
            error = reply.get("error", "unknown error")
            if "not connected" in error or "disconnected" in error:
                raise IMessageUnavailable(error)
            raise IMessageError(error)
        # `ok` and `id` are envelope, not payload.
        return {k: v for k, v in reply.items() if k not in ("ok", "id")}

    # -- commands --------------------------------------------------------
    #
    # A chat is identified by its participant list, because that *is* its
    # identity in iMessage — there's no server-side chat id to quote back.
    # `guid` pins a specific group thread when several share participants.

    def status(self) -> dict:
        """Handles, plus any warning about the registration nearing expiry."""
        return self.request("status")

    def handles(self) -> list[str]:
        """Our own addresses, e.g. `tel:+1...` and `mailto:...`."""
        return self.request("handles").get("handles", [])

    def send(
        self,
        participants: list[str],
        text: str,
        *,
        name: str | None = None,
        guid: str | None = None,
        subject: str | None = None,
        reply_guid: str | None = None,
        reply_part: str | None = None,
        effect: str | None = None,
    ) -> str:
        """Send a message; returns its guid.

        The guid comes back as soon as Apple accepts the message, before
        per-recipient delivery completes, so the UI can draw the bubble
        immediately and update it from later events.
        """
        return self.request(
            "send",
            chat=self._chat(participants, name, guid),
            text=text,
            subject=subject,
            reply_guid=reply_guid,
            reply_part=reply_part,
            effect=effect,
        )["guid"]

    # Attachments can be tens of megabytes over a link we don't control.
    DOWNLOAD_TIMEOUT = 300.0

    def download_attachment(self, guid: str, index: int, dest: str) -> dict:
        """Fetch one attachment's bytes to `dest`; returns {path, bytes}.

        Named by message guid and position rather than by passing the
        attachment object: its MMCS locator and key are byte arrays that JSON
        cannot carry back to the helper, so the helper holds them and we refer
        to them. `index` counts attachments within the message, in the order
        the bridge produced them.

        Safe to call from a worker thread, and meant to be: this blocks for as
        long as the transfer takes, and the daemon's caller is the GLib main
        loop.
        """
        return self.request(
            "download_attachment", guid=guid, index=index, dest=dest,
            _timeout=self.DOWNLOAD_TIMEOUT,
        )

    def react(
        self,
        participants: list[str],
        target_guid: str,
        *,
        kind: str | None = None,
        emoji: str | None = None,
        target_text: str = "",
        target_part: int | None = None,
        enable: bool = True,
        name: str | None = None,
        guid: str | None = None,
    ) -> str:
        """Add or remove a tapback.

        `kind` is one of heart/like/dislike/laugh/emphasize/question, or pass
        `emoji` for an arbitrary-emoji tapback. `target_text` is the text of
        the message being reacted to — iMessage carries it so recipients can
        render 'Loved "…"' without looking it up.
        """
        return self.request(
            "react",
            chat=self._chat(participants, name, guid),
            target_guid=target_guid,
            target_text=target_text,
            # Part 0 = the text body of a normal message. Omitting it makes
            # rustpush write a bare target uuid, which never attaches as a
            # tapback on the phone or any other client.
            target_part=0 if target_part is None else target_part,
            kind=kind,
            emoji=emoji,
            enable=enable,
        )["guid"]

    def edit(
        self,
        participants: list[str],
        target_guid: str,
        text: str,
        *,
        part: int = 0,
        name: str | None = None,
        guid: str | None = None,
    ) -> str:
        return self.request(
            "edit",
            chat=self._chat(participants, name, guid),
            target_guid=target_guid,
            text=text,
            part=part,
        )["guid"]

    def unsend(
        self,
        participants: list[str],
        target_guid: str,
        *,
        part: int = 0,
        name: str | None = None,
        guid: str | None = None,
    ) -> str:
        return self.request(
            "unsend",
            chat=self._chat(participants, name, guid),
            target_guid=target_guid,
            part=part,
        )["guid"]

    def typing(
        self,
        participants: list[str],
        typing: bool,
        *,
        name: str | None = None,
        guid: str | None = None,
    ) -> str:
        return self.request(
            "typing", chat=self._chat(participants, name, guid), typing=typing
        )["guid"]

    def mark_read(
        self,
        participants: list[str],
        *,
        name: str | None = None,
        guid: str | None = None,
    ) -> str:
        """Mark the thread read on the user's other Apple devices too."""
        return self.request("mark_read", chat=self._chat(participants, name, guid))[
            "guid"
        ]

    def rename(
        self,
        participants: list[str],
        new_name: str,
        *,
        guid: str | None = None,
    ) -> str:
        return self.request(
            "rename", chat=self._chat(participants, None, guid), name=new_name
        )["guid"]

    def set_participants(
        self,
        participants: list[str],
        new_participants: list[str],
        *,
        group_version: int = 0,
        name: str | None = None,
        guid: str | None = None,
    ) -> str:
        """Replace a group's participant list.

        iMessage models both adding and removing this way — there is no
        add/remove verb, only a new list plus the version it replaces.
        """
        return self.request(
            "set_participants",
            chat=self._chat(participants, name, guid),
            participants=new_participants,
            group_version=group_version,
        )["guid"]

    @staticmethod
    def _chat(
        participants: list[str], name: str | None, guid: str | None
    ) -> dict:
        return {"participants": participants, "name": name, "guid": guid}

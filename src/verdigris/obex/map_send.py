"""MAP outgoing message send (SMS + iMessage on iOS 26.5).

Per spike/07_map_send.py and spike/RESULTS.md §6, MessageAccess1.PushMessage
with a properly-formed bMessage works on iOS 26.5 — and when the recipient
is iMessage-capable, iOS routes the outgoing as iMessage automatically
(blue bubble). The same code path handles SMS too.

Public surface:
    send_message(session_path, recipient_phone, body) -> str (transfer path)

Caller (typically the daemon's DBus service) owns the MAP session and
just passes its path here.
"""
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

import dbus
import dbus.exceptions

from verdigris.bus import obex

log = logging.getLogger(__name__)


def _byte_stuff(body: str) -> str:
    """Apply MAP bMessage byte-stuffing.

    Lines in the message body that start with `BEGIN:`, `END:`, or other
    keywords the bMessage parser would intercept must be prefixed with a
    single space. Conservative implementation: prefix any line starting
    with `BEGIN:` or `END:`.
    """
    return "\n".join(
        (" " + line) if line.startswith(("BEGIN:", "END:")) else line
        for line in body.splitlines()
    )


def build_bmessage(recipient: str, body: str) -> str:
    """Return a complete bMessage suitable for MAP PushMessage.

    Structure per Bluetooth MAP 1.4 spec:
      • Originator VCARD (empty for outgoing — iPhone fills in)
      • BENV → recipient VCARD + BBODY → MSG body
    """
    stuffed = _byte_stuff(body)
    encoded_len = len(stuffed.encode("utf-8"))
    lines = [
        "BEGIN:BMSG",
        "VERSION:1.0",
        "STATUS:UNREAD",
        "TYPE:SMS_GSM",
        "FOLDER:telecom/msg/outbox",
        # Originator
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        "TEL:",
        "END:VCARD",
        "BEGIN:BENV",
        # Recipient
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N:;;;;",
        f"TEL:{recipient}",
        "END:VCARD",
        "BEGIN:BBODY",
        "CHARSET:UTF-8",
        f"LENGTH:{encoded_len}",
        "BEGIN:MSG",
        stuffed,
        "END:MSG",
        "END:BBODY",
        "END:BENV",
        "END:BMSG",
    ]
    return "\r\n".join(lines) + "\r\n"


def send_message(
    session_path: str,
    recipient: str,
    body: str,
    *,
    folder: str = "telecom/msg/outbox",
    poll_timeout_s: float = 30.0,
) -> str:
    """Push a message via the given MAP session.

    Returns the BlueZ transfer object path once status reaches 'complete'
    or 'gone'. Raises on InvalidArguments or transfer error.
    """
    bmsg = build_bmessage(recipient, body)
    tmp = Path(tempfile.mkstemp(prefix="ibridge_send_", suffix=".bmsg")[1])
    tmp.write_text(bmsg, encoding="utf-8")
    try:
        map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")
        log.info("PushMessage → %s (%d bytes body, folder=%s)",
                 recipient, len(body), folder)
        try:
            ret = map_iface.PushMessage(str(tmp), folder, {})
        except dbus.exceptions.DBusException as e:
            raise RuntimeError(
                f"PushMessage rejected: {e.get_dbus_name()}: "
                f"{e.get_dbus_message() or '(no message)'}"
            ) from e
        transfer_path = str(ret[0]) if isinstance(ret, (tuple, list)) else str(ret)
        log.info("transfer: %s", transfer_path)

        # Poll transfer to completion
        tprops = obex(transfer_path, "org.freedesktop.DBus.Properties")
        deadline = time.time() + poll_timeout_s
        status: str | None = None
        while time.time() < deadline:
            try:
                status = str(tprops.Get("org.bluez.obex.Transfer1", "Status"))
            except dbus.exceptions.DBusException:
                status = "gone"
                break
            if status in ("complete", "error"):
                break
            time.sleep(0.1)
        log.info("send result: status=%s", status)
        if status == "error":
            raise RuntimeError(f"Transfer reported error: {transfer_path}")
        return transfer_path
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

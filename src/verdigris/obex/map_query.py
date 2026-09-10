"""Direct MAP queries against the iPhone — list recent messages from a
folder, fetch full message bodies on demand.

Used by the DBus service so the CLI can show the iPhone's actual inbox
history (not just JSONL events since daemon startup).
"""
from __future__ import annotations

import logging
from typing import Any

import dbus
import dbus.exceptions

from verdigris.bus import obex
from verdigris.events import normalize_phone, parse_map_timestamp

log = logging.getLogger(__name__)


def _navigate_to_folder(map_iface: dbus.Interface, folder: str) -> None:
    """SetFolder to an absolute folder like 'telecom/msg/INBOX'."""
    try:
        map_iface.SetFolder("/")
    except dbus.exceptions.DBusException:
        pass
    for seg in folder.split("/"):
        if not seg:
            continue
        map_iface.SetFolder(seg)


def list_recent_messages(
    session_path: str,
    folder: str = "telecom/msg/INBOX",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent `limit` messages in `folder` as normalized dicts.

    Each dict has: handle, sender, sender_phone, sender_phone_norm,
    body, timestamp, read, status, type, folder.
    """
    map_iface = obex(session_path, "org.bluez.obex.MessageAccess1")
    _navigate_to_folder(map_iface, folder)

    # ListMessages returns object paths; we fetch each one's Message1 props
    paths = list(map_iface.ListMessages(
        "", {"MaxListCount": dbus.UInt16(limit)}
    ))
    log.info("ListMessages(%s, limit=%d) → %d paths", folder, limit, len(paths))

    out: list[dict[str, Any]] = []
    for p in paths:
        path_s = str(p)
        try:
            raw = dict(
                obex(path_s, "org.freedesktop.DBus.Properties")
                .GetAll("org.bluez.obex.Message1")
            )
        except dbus.exceptions.DBusException as e:
            log.debug("skip %s: %s", path_s, e.get_dbus_name())
            continue
        sender_raw = raw.get("Sender") or raw.get("SenderAddress")
        sender_raw = str(sender_raw) if sender_raw is not None else None
        ts = parse_map_timestamp(raw.get("Timestamp"))
        out.append({
            "handle": path_s.rsplit("/", 1)[-1],
            "sender": sender_raw or "",
            "sender_phone_norm": normalize_phone(sender_raw) or "",
            "body": str(raw.get("Subject", "")) or "",
            "timestamp": ts.isoformat() if ts else "",
            "read": bool(raw.get("Read", False)),
            "status": str(raw.get("Status", "")),
            "type": str(raw.get("Type", "")),
            "folder": str(raw.get("Folder", "")) or folder,
        })
    return out

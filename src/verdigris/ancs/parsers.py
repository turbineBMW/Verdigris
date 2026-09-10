"""ANCS wire-format parsers + builders.

Ported from bmh129/ancs4linux/observer/ancs/{parsers,builders}.py
(GPL-2.0-compatible). Pure functions over bytes — unit-testable without
DBus / BlueZ.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from verdigris.ancs.constants import (
    USHORT_MAX,
    CommandID,
    EventFlag,
    EventID,
    NotificationAttributeID,
)

# ---- inbound parsers -----------------------------------------------------

def parse_attr_string(data: bytearray) -> tuple[str, bytearray]:
    """Pull one variable-length ANCS attribute string off the front of data.

    Wire format: [AttributeID: u8][Length: u16-le][bytes...]
    Caller already knows the attribute type, so type is consumed but ignored.
    """
    if len(data) < 3:
        raise ValueError("short attribute header")
    _attr_id, size = struct.unpack("<BH", bytes(data[:3]))
    body = bytes(data[3:3 + size])
    if len(body) < size:
        raise ValueError("truncated attribute body")
    rest = data[3 + size:]
    return body.decode("utf-8", errors="replace"), rest


@dataclass(slots=True)
class Notification:
    """8-byte Notification Source packet from the iPhone."""
    id: int
    type: int    # EventID
    flags: int   # bitmask of EventFlag
    category: int
    category_count: int

    @classmethod
    def parse(cls, data: bytes) -> Notification:
        if len(data) < 8:
            raise ValueError(f"NS packet too short ({len(data)} bytes)")
        eid, flags, cat, count, uid = struct.unpack("<BBBBI", bytes(data[:8]))
        return cls(id=uid, type=eid, flags=flags,
                   category=cat, category_count=count)

    @property
    def is_preexisting(self) -> bool:
        return bool(self.flags & EventFlag.PreExisting)

    @property
    def is_fresh(self) -> bool:
        return not self.is_preexisting

    @property
    def is_silent(self) -> bool:
        return bool(self.flags & EventFlag.Silent)

    @property
    def has_positive_action(self) -> bool:
        return bool(self.flags & EventFlag.PositiveAction)

    @property
    def has_negative_action(self) -> bool:
        return bool(self.flags & EventFlag.NegativeAction)


@dataclass(slots=True)
class NotificationAttributes:
    """Body of a CommandID.GetNotificationAttributes response on Data Source."""
    id: int
    app_id: str
    title: str
    subtitle: str
    message: str
    positive_action: str | None
    negative_action: str | None

    @classmethod
    def parse(cls, body: bytes) -> NotificationAttributes:
        msg = bytearray(body)
        if len(msg) < 4:
            raise ValueError("attrs response too short")
        uid = struct.unpack("<I", bytes(msg[:4]))[0]
        msg = msg[4:]
        # We always request: AppIdentifier, Title, Subtitle, Message,
        # PositiveActionLabel?, NegativeActionLabel?
        app_id, msg = parse_attr_string(msg)
        title, msg = parse_attr_string(msg)
        subtitle, msg = parse_attr_string(msg)
        message, msg = parse_attr_string(msg)
        pos = neg = None
        if msg and msg[0] == NotificationAttributeID.PositiveActionLabel:
            pos, msg = parse_attr_string(msg)
        if msg and msg[0] == NotificationAttributeID.NegativeActionLabel:
            neg, msg = parse_attr_string(msg)
        return cls(
            id=uid, app_id=app_id, title=title, subtitle=subtitle,
            message=message, positive_action=pos, negative_action=neg,
        )


@dataclass(slots=True)
class AppAttributes:
    """Body of a CommandID.GetAppAttributes response on Data Source.

    Layout differs from NotificationAttributes: app_id is a NUL-terminated
    C string, NOT a length-prefixed ANCS attribute. Then comes the DisplayName
    attribute in normal ANCS format.
    """
    app_id: str
    app_name: str

    @classmethod
    def parse(cls, body: bytes) -> AppAttributes:
        msg = bytearray(body)
        if b"\0" not in msg:
            raise ValueError("AppAttributes: missing NUL after app_id")
        app_id_bytes, _, rest = bytes(msg).partition(b"\0")
        app_id = app_id_bytes.decode("utf-8", errors="replace")
        if not rest:
            return cls(app_id=app_id, app_name="<not installed>")
        # Now an ANCS-format attribute: [AttrID][Length:u16-le][bytes]
        _, size = struct.unpack("<BH", rest[:3])
        body_bytes = rest[3:3 + size]
        app_name = body_bytes.decode("utf-8", errors="replace")
        return cls(app_id=app_id, app_name=app_name)


@dataclass(slots=True)
class DataSourceEvent:
    """One inbound packet on the Data Source characteristic."""
    type: int      # CommandID
    body: bytes

    @classmethod
    def parse(cls, data: bytes) -> DataSourceEvent:
        if not data:
            raise ValueError("DS packet empty")
        return cls(type=data[0], body=bytes(data[1:]))


# ---- outbound builders ---------------------------------------------------

def build_get_notification_attributes(
    notification_id: int,
    *,
    title_max: int = 64,
    subtitle_max: int = 64,
    message_max: int = 256,
    want_positive: bool = False,
    want_negative: bool = False,
) -> bytes:
    """Construct a Control Point write asking for an incoming notification's
    full attributes. Variable-length attributes need a u16-le maximum size.
    """
    title_max = max(0, min(title_max, USHORT_MAX))
    subtitle_max = max(0, min(subtitle_max, USHORT_MAX))
    message_max = max(0, min(message_max, USHORT_MAX))
    out = bytearray()
    out.append(CommandID.GetNotificationAttributes)
    out += struct.pack("<I", notification_id)
    out.append(NotificationAttributeID.AppIdentifier)             # no maxlen
    out.append(NotificationAttributeID.Title)
    out += struct.pack("<H", title_max)
    out.append(NotificationAttributeID.Subtitle)
    out += struct.pack("<H", subtitle_max)
    out.append(NotificationAttributeID.Message)
    out += struct.pack("<H", message_max)
    if want_positive:
        out.append(NotificationAttributeID.PositiveActionLabel)
    if want_negative:
        out.append(NotificationAttributeID.NegativeActionLabel)
    return bytes(out)


def build_get_app_attributes(app_id: str) -> bytes:
    """Construct a Control Point write asking for a NUL-terminated app_id's
    human-readable display name."""
    out = bytearray()
    out.append(CommandID.GetAppAttributes)
    out += app_id.encode("utf-8") + b"\0"
    from verdigris.ancs.constants import AppAttributeID
    out.append(AppAttributeID.DisplayName)
    return bytes(out)


def build_perform_action(notification_id: int, is_positive: bool) -> bytes:
    """Construct a Control Point write to invoke a positive/negative action
    on a notification (Apple's term — iMessage's 'Reply' is positive, etc.)."""
    from verdigris.ancs.constants import ActionID
    out = bytearray()
    out.append(CommandID.PerformNotificationAction)
    out += struct.pack("<I", notification_id)
    out.append(ActionID.Positive if is_positive else ActionID.Negative)
    return bytes(out)


# Re-export EventID/EventFlag for callers
__all__ = [
    "AppAttributes",
    "DataSourceEvent",
    "EventFlag",
    "EventID",
    "Notification",
    "NotificationAttributes",
    "build_get_app_attributes",
    "build_get_notification_attributes",
    "build_perform_action",
]

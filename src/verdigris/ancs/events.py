"""AncsEvent — normalized per-app iPhone notification.

Distinct from SmsEvent (which represents MAP-delivered SMS/iMessage) so
sinks can render them differently. Both flow through the same sink fan-out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class AncsEvent:
    """A single per-app notification mirrored from an iPhone via ANCS."""

    # Identity
    notification_id: int          # ANCS uid (4-byte, device-local)
    device_path: str              # /org/bluez/hciN/dev_XX_…

    # Source app
    app_id: str                   # bundle id, e.g. "com.apple.MobileSMS"
    app_name: str                 # display name, may be empty until resolved

    # Content
    title: str
    subtitle: str
    body: str

    # Classification
    category: str                 # e.g. "Social", "IncomingCall"
    is_silent: bool
    is_preexisting: bool          # True if iPhone is replaying an already-shown notif

    # Action labels (if the source app declared them)
    positive_action: str | None
    negative_action: str | None

    seen_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def display_title(self) -> str:
        """Best human-readable title."""
        if self.title:
            return self.title
        return self.app_name or self.app_id or "Notification"

    def to_dict(self) -> dict:
        return {
            "kind": "ancs_notification",
            "notification_id": self.notification_id,
            "device_path": self.device_path,
            "app_id": self.app_id,
            "app_name": self.app_name,
            "title": self.title,
            "subtitle": self.subtitle,
            "body": self.body,
            "category": self.category,
            "is_silent": self.is_silent,
            "is_preexisting": self.is_preexisting,
            "positive_action": self.positive_action,
            "negative_action": self.negative_action,
            "seen_at": self.seen_at.isoformat(),
        }

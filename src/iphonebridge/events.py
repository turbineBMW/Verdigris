"""Normalized event types emitted by the OBEX layer.

The iPhone's MAP server speaks bMessages and proprietary metadata;
the rest of the daemon shouldn't have to care. Everything upstream
sees these simple dataclasses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

# ---- helpers ------------------------------------------------------------

_PHONE_KEEP = re.compile(r"\D")

def normalize_phone(raw: str | None) -> str | None:
    """Reduce a phone string to digits only (E.164-ish minus the +).

    "+1 (561) 555-0106" → "15615550106"
    "5615550106"        → "5615550106"
    "Mom"               → None  (looked like a name)
    """
    if not raw:
        return None
    digits = _PHONE_KEEP.sub("", raw)
    # 7+ digits is plausibly a phone number
    return digits if len(digits) >= 7 else None


def parse_map_timestamp(ts: str | None) -> datetime | None:
    """Parse MAP's timestamp format: '20260519T181423' or with timezone suffix."""
    if not ts:
        return None
    # MAP timestamps: YYYYMMDDTHHMMSS, optionally followed by a TZ offset
    base = ts[:15]
    try:
        dt = datetime.strptime(base, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    # MAP timestamps are local-time on the iPhone; we'll treat as local
    return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


# ---- reaction (tapback) detection ----------------------------------------
#
# Bluetooth MAP has no structured concept of an iMessage tapback — iOS just
# synthesizes a plain-text notification body for it ('Loved "hey there"').
# We can only detect these by pattern-matching that phrasing; there's no
# reliable link back to which prior message it targets beyond the quoted
# snippet iOS includes, and no way to *send* a real tapback over MAP at all
# (PushMessage only supports fresh plain-text messages).

_REACTION_VERBS = (
    "Loved", "Liked", "Disliked", "Laughed at", "Emphasized", "Questioned",
    "Removed a heart from", "Removed a like from", "Removed a dislike from",
    "Removed a laugh from", "Removed an exclamation from",
    "Removed a question mark from",
)
_REACTION_RE = re.compile(
    r"^(?P<verb>" + "|".join(_REACTION_VERBS) + r") [“\"](?P<snippet>.*)[”\"]$",
    re.DOTALL,
)


# iOS 18+ also allows an arbitrary emoji as a tapback, which arrives as
# 'Reacted 😀 to "…"' rather than one of the six fixed verbs above.
_REACTION_EMOJI_RE = re.compile(
    r"^Reacted (?P<emoji>.+?) to [“\"](?P<snippet>.*)[”\"]$",
    re.DOTALL,
)


def _detect_reaction(body: str | None) -> tuple[str | None, str | None]:
    """Return (verb, quoted snippet), or (None, None) if not a tapback.

    For an emoji tapback the verb is "Reacted <emoji>"; callers split on the
    prefix to get the emoji, since there's no fixed icon for those.
    """
    if not body:
        return None, None
    body = body.strip()
    m = _REACTION_RE.match(body)
    if m:
        return m.group("verb"), m.group("snippet")
    m = _REACTION_EMOJI_RE.match(body)
    if m:
        return f"Reacted {m.group('emoji')}", m.group("snippet")
    return None, None


# ---- event types --------------------------------------------------------

EventKind = Literal["sms_received", "sms_seen", "sms_sent"]


@dataclass(slots=True)
class SmsEvent:
    """A single SMS message event from the iPhone via MAP."""

    kind: EventKind
    handle: str                 # BlueZ obex Message1 path tail, e.g. "message93446842893444124"
    sender_phone: str | None    # raw, as given by MAP
    sender_phone_norm: str | None  # digits-only, for contacts lookup
    contact_name: str | None    # resolved from contacts cache, may be None
    body: str | None            # MAP puts the SMS text in `Subject`
    timestamp: datetime | None
    is_read: bool
    raw_status: str | None
    raw_type: str | None
    # Full BlueZ obex DBus path to the Message1 object, so downstream
    # code (e.g. libnotify sink) can write back read-state.
    message_path: str | None = None
    # Group-chat identity. Set only by transports that actually know it —
    # iMessage does, MAP does not. Threading keys on this when present, so a
    # group message files under the conversation rather than scattering into
    # each participant's 1:1 thread.
    #
    # Part of the event rather than the iMessage-only extras because it
    # reaches disk: history reloaded from events.jsonl has to thread the same
    # way it did live.
    chat_guid: str | None = None
    chat_name: str | None = None
    # iMessage's own per-message id. Every action the UI offers (tapback,
    # reply, edit, unsend) names its target by this, and delivery receipts
    # arrive keyed to it — so it has to persist to disk, or a message stops
    # being actionable the moment the app restarts and reloads from history.
    guid: str | None = None
    # The message this one replies to. Here rather than in the iMessage-only
    # extras for the same reason as chat_guid: it reaches disk, and a reply
    # reloaded from history has to still read as a reply instead of turning
    # back into an ordinary message on the next restart.
    reply_to_guid: str | None = None
    seen_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # Derived, not passed by callers — see _detect_reaction() above.
    reaction_verb: str | None = field(default=None, init=False)
    reaction_snippet: str | None = field(default=None, init=False)
    # Exact message a tapback targets. MAP never has this (only a quoted
    # snippet); the native transport does, and without it on the event itself
    # the target is lost the moment the event hits a sink — extras ride only
    # the D-Bus bus. Native reactions also arrive with empty to_text, so the
    # snippet fallback cannot recover them after a restart.
    reaction_target_guid: str | None = None

    def __post_init__(self) -> None:
        self.reaction_verb, self.reaction_snippet = _detect_reaction(self.body)

    @property
    def display_sender(self) -> str:
        """Best name we have for the sender."""
        return self.contact_name or self.sender_phone or "(unknown)"

    def to_dict(self) -> dict:
        """Serializable form for JSONL log."""
        return {
            "kind": self.kind,
            "handle": self.handle,
            "sender_phone": self.sender_phone,
            "sender_phone_norm": self.sender_phone_norm,
            "contact_name": self.contact_name,
            "body": self.body,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "is_read": self.is_read,
            "raw_status": self.raw_status,
            "raw_type": self.raw_type,
            "chat_guid": self.chat_guid,
            "chat_name": self.chat_name,
            "guid": self.guid,
            "reply_to_guid": self.reply_to_guid,
            "seen_at": self.seen_at.isoformat(),
            "reaction_verb": self.reaction_verb,
            "reaction_snippet": self.reaction_snippet,
            "reaction_target_guid": self.reaction_target_guid,
        }


def sms_sent_event(
    recipient: str,
    body: str,
    *,
    contact_name: str | None = None,
    transfer_path: str = "",
    guid: str | None = None,
    reply_to_guid: str | None = None,
) -> SmsEvent:
    """Build an SmsEvent for a message *we* just sent via MAP PushMessage.

    For a sent message the relevant party is the recipient, so the
    `sender_*` / `contact_name` fields carry the recipient — that keeps it
    in the same conversation thread as incoming messages from that person.
    """
    # The transfer path tail alone ("transfer3") is NOT unique: obexd numbers
    # transfers per session starting at zero, so the counter resets every time
    # a session is recreated and handles collide across restarts. Consumers
    # dedupe by handle, so a colliding handle makes a real message vanish.
    # Stamp it to keep handles unique for the lifetime of the log.
    stamp = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
    tail = transfer_path.rsplit("/", 1)[-1] if transfer_path else "sent"
    handle = f"{tail}-{stamp}"
    return SmsEvent(
        kind="sms_sent",
        handle=handle,
        sender_phone=recipient,
        sender_phone_norm=normalize_phone(recipient),
        contact_name=contact_name,
        body=body,
        timestamp=datetime.now().astimezone(),
        is_read=True,
        guid=guid,
        reply_to_guid=reply_to_guid,
        raw_status="sent",
        raw_type="sms_sent",
        message_path=None,
    )


def sms_event_from_message1_props(
    handle: str, props: dict, contact_name: str | None = None,
) -> SmsEvent:
    """Construct an SmsEvent from BlueZ's org.bluez.obex.Message1 properties.

    See spike/RESULTS.md §3 — the SMS body comes from `Subject`.
    """
    sender_raw = props.get("Sender") or props.get("SenderAddress")
    sender_raw = str(sender_raw) if sender_raw is not None else None
    norm = normalize_phone(sender_raw)
    return SmsEvent(
        kind="sms_received",
        handle=handle,
        sender_phone=sender_raw,
        sender_phone_norm=norm,
        contact_name=contact_name,
        body=str(props.get("Subject", "")) or None,
        timestamp=parse_map_timestamp(props.get("Timestamp")),
        is_read=bool(props.get("Read", False)),
        raw_status=str(props.get("Status", "")) or None,
        raw_type=str(props.get("Type", "")) or None,
    )

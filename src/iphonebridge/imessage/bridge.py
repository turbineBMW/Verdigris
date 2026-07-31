"""Turning `ib-imessage` events into the daemon's existing event objects.

The point of translating rather than introducing a parallel event type is that
the daemon, the JSONL history, the libnotify sink and the Qt UI all already
speak `SmsEvent`. Reusing it means iMessage traffic appears everywhere MAP
traffic does, with no changes to the UI at all.

The translation is genuinely lossy, and deliberately so — `SmsEvent` was
shaped around what MAP can express. What iMessage carries and `SmsEvent`
cannot hold (real tapback targets, reply threading, attachments, edits) is
preserved on the side in `IMessageExtras`, so the richer UI work can use it
without a second event pipeline in the meantime.

rustpush's `Message` is a serde-tagged enum, so an event body arrives as a
single-key dict: `{"Message": {...}}`, `{"React": {...}}`, `{"Delivered":
null}` and so on. Variants we can't render are dropped rather than guessed at.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..events import SmsEvent, normalize_phone
from .handles import from_handle, group_key

log = logging.getLogger(__name__)

# Apple timestamps in rustpush's `sent_timestamp` are milliseconds since the
# Unix epoch, unlike sms.db's Core Data offsets — no 2001 epoch here.
_MS = 1000.0

# Tapback verbs, matching the phrasing MAP synthesizes for the same thing, so
# reactions from either transport render identically.
_REACTION_VERBS = {
    "Heart": "Loved",
    "Like": "Liked",
    "Dislike": "Disliked",
    "Laugh": "Laughed at",
    "Emphasize": "Emphasized",
    "Question": "Questioned",
}


@dataclass
class IMessageExtras:
    """iMessage detail that `SmsEvent` has nowhere to put.

    Keyed off the message guid so a consumer can look it up when rendering.
    """

    guid: str
    outgoing: bool
    service: str = "iMessage"
    # The handle this came from. Needed to route typing indicators and
    # receipts to a conversation: `chat_participants` includes our own
    # handle, so its first entry is not reliably the other party.
    sender: str | None = None
    chat_participants: list[str] = field(default_factory=list)
    chat_name: str | None = None
    is_group: bool = False
    # The exact message a tapback targets — MAP can only offer a quoted
    # snippet, which is why tapbacks are unreliable on that path.
    reaction_target_guid: str | None = None
    reaction_verb: str | None = None
    # Real reply threading, likewise absent from MAP.
    reply_to_guid: str | None = None
    edited_from_guid: str | None = None
    unsent: bool = False
    attachments: list[dict] = field(default_factory=list)


@dataclass
class Translated:
    """What one helper event became."""

    event: SmsEvent | None
    extras: IMessageExtras | None
    # Set for events that aren't messages at all — delivery/read receipts and
    # typing indicators. The daemon uses these to update existing rows rather
    # than to append new ones.
    receipt: str | None = None
    typing: bool | None = None


def _variant(message) -> tuple[str, object]:
    """Split a serde-tagged enum into (variant name, payload)."""
    if isinstance(message, str):
        # Unit variants can serialize as a bare string.
        return message, None
    if isinstance(message, dict) and len(message) == 1:
        name, payload = next(iter(message.items()))
        return name, payload
    return "", None


def _timestamp(inst: dict) -> datetime | None:
    raw = inst.get("sent_timestamp")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(float(raw) / _MS, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        # A nonsense timestamp shouldn't cost us the message body.
        log.debug("unparseable sent_timestamp %r", raw)
        return None


def _text_from_parts(parts) -> tuple[str, list[dict]]:
    """Flatten `MessageParts` into display text plus attachment descriptors.

    A body is a list of parts, each an enum: `{"Text": ["hi", {...}]}`,
    `{"Attachment": {...}}`, `{"Mention": ["+1555…", "Bob"]}`. Mentions render
    as the display name, which is what the Messages app shows.
    """
    chunks: list[str] = []
    attachments: list[dict] = []
    if not isinstance(parts, list):
        return "", attachments
    for entry in parts:
        part = entry.get("part") if isinstance(entry, dict) else None
        name, payload = _variant(part)
        if name == "Text" and isinstance(payload, list) and payload:
            chunks.append(str(payload[0]))
        elif name == "Mention" and isinstance(payload, list) and len(payload) > 1:
            chunks.append(str(payload[1]))
        elif name == "Attachment" and isinstance(payload, dict):
            attachments.append(
                {
                    "name": payload.get("name"),
                    "mime_type": payload.get("mime"),
                    "uti": payload.get("uti"),
                    "size": payload.get("size"),
                }
            )
        elif name == "Object":
            # Inline media is already represented in the Text part as U+F00A
            # (the live "3-0 on my return" form). The attachment entry
            # carries the bytes; nothing to splice into the body here.
            continue
    return "".join(chunks), attachments


def translate(event: dict, my_handles: set[str]) -> Translated | None:
    """Convert one helper event. Returns None for anything not worth surfacing."""
    if event.get("event") != "message":
        return None
    inst = event.get("inst")
    if not isinstance(inst, dict):
        return None

    guid = inst.get("id") or ""
    sender = inst.get("sender") or None
    conversation = inst.get("conversation") or {}
    participants = [p for p in conversation.get("participants", []) if p]
    # A message is ours if it came from one of our own registered handles.
    # This is the only reliable test: iMessage delivers our own sent messages
    # to our other devices, so direction can't be inferred from arrival.
    outgoing = bool(sender and sender in my_handles)

    name, payload = _variant(inst.get("message"))

    extras = IMessageExtras(
        guid=guid,
        outgoing=outgoing,
        sender=sender,
        chat_participants=participants,
        chat_name=conversation.get("cv_name"),
        # iMessage counts our own handle among the participants, so a
        # one-to-one chat has two and anything larger is a group.
        is_group=len(participants) > 2,
    )

    # -- receipts and indicators: no new message, just state changes ------
    if name in ("Delivered", "Read", "MessageReadOnDevice"):
        return Translated(event=None, extras=extras, receipt=name)
    if name == "Typing":
        typing = bool(payload[0]) if isinstance(payload, list) and payload else False
        return Translated(event=None, extras=extras, typing=typing)

    body: str | None = None

    if name == "Message" and isinstance(payload, dict):
        body, attachments = _text_from_parts(payload.get("parts"))
        extras.attachments = attachments
        extras.reply_to_guid = payload.get("reply_guid")
        subject = payload.get("subject")
        if subject:
            # iMessage shows the subject as a bold first line.
            body = f"{subject}\n{body}" if body else subject
        if not body and attachments:
            # Give the notification and history something to show for an
            # attachment-only message rather than an empty bubble.
            first = attachments[0].get("name") or "attachment"
            body = f"[{first}]"

    elif name == "React" and isinstance(payload, dict):
        extras.reaction_target_guid = payload.get("to_uuid")
        reaction_name, reaction_payload = _variant(
            (payload.get("reaction") or {}).get("React", {}).get("reaction")
            if isinstance(payload.get("reaction"), dict)
            else None
        )
        verb = _REACTION_VERBS.get(reaction_name)
        if reaction_name == "Emoji" and reaction_payload:
            # No trailing " to": the verb is what the UI reads the emoji out
            # of, and MAP's spelling stops at the emoji. Carrying the whole
            # clause here put a stray "to" on the badge.
            verb = f"Reacted {reaction_payload}"
        extras.reaction_verb = verb
        target = payload.get("to_text") or ""
        # Phrased exactly like MAP's synthesized text so `_detect_reaction`
        # in events.py picks it up and both transports render the same. The
        # emoji form takes the "to" the verb no longer carries.
        lead = f"{verb} to" if reaction_name == "Emoji" and verb else verb
        lead = lead or "Reacted to"
        body = f'{lead} “{target}”'

    elif name == "Edit" and isinstance(payload, dict):
        extras.edited_from_guid = payload.get("tuuid")
        body, _ = _text_from_parts(payload.get("new_parts"))

    elif name == "Unsend" and isinstance(payload, dict):
        extras.unsent = True
        extras.edited_from_guid = payload.get("tuuid")
        body = "[message unsent]"

    elif name == "RenameMessage" and isinstance(payload, dict):
        body = f'[renamed the conversation to "{payload.get("new_name", "")}"]'

    elif name == "ChangeParticipants":
        body = "[changed the participants]"

    else:
        # Everything else — profile shares, cache invalidations, scheduling
        # chatter. Not errors, just not renderable.
        log.debug("ignoring iMessage variant %r", name or "<unknown>")
        return None

    if outgoing:
        # Not participants[0]: our own handle is in that list, so a message
        # we sent would be filed under a thread with ourselves.
        others = [p for p in participants if p not in my_handles]
        peer = others[0] if others else sender
    else:
        peer = sender
    peer_display = from_handle(peer) if peer else None

    sms = SmsEvent(
        # EventKind is a Literal of strings, not an enum. "sms_sent" for our
        # own messages echoed from another Apple device, so they render as
        # ours rather than as something we received from ourselves.
        kind="sms_sent" if outgoing else "sms_received",
        # MAP uses the obex object path tail as a handle; the guid is the
        # equivalent stable per-message identifier here.
        handle=guid,
        sender_phone=peer_display,
        sender_phone_norm=normalize_phone(peer_display),
        contact_name=None,  # the daemon resolves this against its cache
        body=body,
        timestamp=_timestamp(inst),
        # Anything arriving live is unread by definition; our own messages
        # echoed from another device are not "unread" in any useful sense.
        is_read=outgoing,
        raw_status=name,
        raw_type="iMessage",
        message_path=None,
        guid=guid,
        # Only for groups. In a 1:1 chat the peer's number is the better key,
        # because it merges with everything MAP and the backup already filed
        # under that person.
        chat_guid=_group_key(participants, my_handles) if extras.is_group else None,
        chat_name=extras.chat_name,
        # Promoted off `extras` deliberately: only fields on the event reach
        # events.jsonl. Left live-only, an incoming reply quoted its target
        # correctly until the app restarted and then reloaded as an ordinary
        # bubble, which reads as replies simply not working.
        reply_to_guid=extras.reply_to_guid,
    )
    return Translated(event=sms, extras=extras)


def _group_key(participants: list[str], my_handles) -> str | None:
    """A stable identifier for a group conversation.

    Delegates to `handles.group_key` so the backup importer computes the
    identical string from `chat_handle_join`. This used to be deliberately
    distinct from the backup's `chat_guid` format, on the grounds that
    matching it meant parsing Apple's identifiers — but the backup's guid is
    local database identity that can't be derived from a live payload at all,
    so the participant set is the only common ground. Keying them apart meant
    a group you'd imported and the same group arriving live showed up as two
    unrelated conversations.
    """
    return group_key(participants, my_handles)

"""Read the iPhone's Messages database (`sms.db`) out of a local backup.

This is the data MAP refuses to hand over. Bluetooth MAP gives us incoming
text only — no messages you sent from the phone, no attachments, no link
from a tapback to the message it targets, no reply threading. All of that
is first-class in sms.db:

    message.is_from_me              → direction, including phone-sent
    message.associated_message_guid → exact tapback target (not a text guess)
    message.thread_originator_guid  → real reply threading
    attachment / message_attachment_join → the actual files

Everything here is read-only; we never write to the database.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Apple stores message dates as nanoseconds since 2001-01-01 UTC (older
# rows use seconds). Anything below this magnitude is the legacy format.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_NS_THRESHOLD = 1_000_000_000_000

# message.associated_message_type → tapback verb. 2000-2005 add a reaction,
# 3000-3005 remove the matching one.
# chat.style: 43 is a group conversation, 45 is one-to-one.
_GROUP_STYLE = 43

_TAPBACK_TYPES = {
    2000: "Loved", 2001: "Liked", 2002: "Disliked",
    2003: "Laughed at", 2004: "Emphasized", 2005: "Questioned",
    3000: "Removed a heart from", 3001: "Removed a like from",
    3002: "Removed a dislike from", 3003: "Removed a laugh from",
    3004: "Removed an exclamation from", 3005: "Removed a question mark from",
}


def apple_time(value) -> datetime | None:
    """Convert a Core Data timestamp to an aware datetime."""
    if not value:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > _NS_THRESHOLD:      # nanoseconds (iOS 11+)
        v /= 1_000_000_000
    return _APPLE_EPOCH + timedelta(seconds=v)


# `attributedBody` is a NeXT typedstream (NSArchiver), *not* a binary plist
# and not an NSKeyedArchiver graph — plistlib can't touch it.
_TYPEDSTREAM_MAGIC = b"\x04\x0bstreamtyped"
# Class reference, then the '+' type code that introduces a length-prefixed
# byte string. That byte string is the NSAttributedString's backing text.
_STRING_MARKER = b"\x84\x01+"

# Attachments and inline media are represented in the text as an object
# replacement character; on its own it isn't a body at all.
_OBJ_REPLACEMENT = "￼"


def _read_length(buf: bytes, i: int) -> tuple[int, int]:
    """Typedstream integer at `buf[i]`, and the offset just past it.

    Small values are stored inline; 0x81/0x82/0x83 introduce a 2-, 4- or
    8-byte little-endian value.
    """
    b = buf[i]
    if b == 0x81:
        return int.from_bytes(buf[i + 1:i + 3], "little"), i + 3
    if b == 0x82:
        return int.from_bytes(buf[i + 1:i + 5], "little"), i + 5
    if b == 0x83:
        return int.from_bytes(buf[i + 1:i + 9], "little"), i + 9
    return b, i + 1


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Pull plain text out of `attributedBody`.

    Newer iOS often leaves `message.text` NULL and stores the body as an
    archived NSAttributedString instead, so a naive reader sees a database
    full of empty messages.

    Only the backing string is read — the attribute runs after it carry
    link/mention/file-transfer metadata we don't render. Reading the whole
    graph would mean implementing the archiver's class and object tables;
    the string is the first '+' value in the stream, which is enough.
    """
    if not blob or not blob.startswith(_TYPEDSTREAM_MAGIC):
        return None
    k = blob.find(_STRING_MARKER)
    if k == -1:
        return None
    n, i = _read_length(blob, k + len(_STRING_MARKER))
    if n <= 0 or i + n > len(blob):
        return None
    text = blob[i:i + n].decode("utf-8", "replace")
    # A body that's nothing but placeholders belongs to an attachment-only
    # message; the attachment itself carries the content.
    if not text.replace(_OBJ_REPLACEMENT, "").strip():
        return None
    return text


@dataclass(slots=True)
class BackupMessage:
    guid: str
    text: str | None
    outgoing: bool
    timestamp: datetime | None
    handle: str | None            # peer phone/email
    chat_id: str | None           # chat identifier (group or 1:1)
    service: str | None           # iMessage / SMS
    is_read: bool
    chat_guid: str | None = None  # local sqlite chat identity — backup-only
    chat_name: str | None = None  # group's display name, when it has one
    # Everyone in the chat but us. `chat_guid` cannot be matched against a
    # live message (it's a phone-local id), so this is what lets an imported
    # group and the same group arriving over iMessage resolve to one thread.
    participants: list[str] = field(default_factory=list)
    is_group: bool = False
    reaction_verb: str | None = None
    reaction_target_guid: str | None = None
    reply_to_guid: str | None = None
    attachments: list[dict] = field(default_factory=list)

    @property
    def is_reaction(self) -> bool:
        return self.reaction_verb is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def read_messages(db_path: str | Path, *, limit: int | None = None
                  ) -> list[BackupMessage]:
    """Read messages newest-last. Tolerates older schemas with missing columns."""
    db_path = Path(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = _columns(con, "message")
        if not cols:
            raise ValueError(f"{db_path} has no `message` table")

        def col(name: str, default: str = "NULL") -> str:
            # Bare expression only — the SELECT below supplies the alias.
            # Returning "NULL AS name" here produced "NULL AS name AS name"
            # on any schema missing the column, which is a syntax error, so
            # the "tolerates older schemas" path failed on exactly the older
            # schemas it exists for.
            return f"m.{name}" if name in cols else default

        sql = f"""
            SELECT m.ROWID AS rowid,
                   m.guid                AS guid,
                   m.text                AS text,
                   {col('attributedBody')} AS attributedBody,
                   m.is_from_me          AS is_from_me,
                   m.date                AS date,
                   {col('is_read')}      AS is_read,
                   {col('service')}      AS service,
                   {col('associated_message_type')} AS associated_message_type,
                   {col('associated_message_guid')} AS associated_message_guid,
                   {col('thread_originator_guid')}  AS thread_originator_guid,
                   h.id                  AS handle,
                   c.ROWID               AS chat_rowid,
                   c.chat_identifier     AS chat_identifier,
                   c.guid                AS chat_guid,
                   c.style               AS chat_style,
                   c.display_name        AS chat_display_name,
                   c.room_name           AS chat_room_name
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN chat c ON c.ROWID = cmj.chat_id
            ORDER BY m.date ASC
        """
        rows = list(con.execute(sql))
        if limit is not None:
            rows = rows[-limit:]

        attachments = _read_attachments(con)
        participants = _read_participants(con)

        out: list[BackupMessage] = []
        for r in rows:
            # `text` can itself be nothing but attachment placeholders.
            text = (r["text"] or "").strip()
            if not text.replace(_OBJ_REPLACEMENT, "").strip():
                text = ""
            text = text or decode_attributed_body(r["attributedBody"])
            atype = r["associated_message_type"] or 0
            verb = _TAPBACK_TYPES.get(int(atype)) if atype else None
            target = r["associated_message_guid"]
            if target:
                # Stored as "p:0/<guid>" or "bp:<guid>" — keep the guid.
                target = target.split("/")[-1]
            out.append(BackupMessage(
                guid=r["guid"],
                text=text,
                outgoing=bool(r["is_from_me"]),
                timestamp=apple_time(r["date"]),
                handle=r["handle"],
                chat_id=r["chat_identifier"],
                chat_guid=r["chat_guid"],
                participants=participants.get(r["chat_rowid"], []),
                chat_name=(r["chat_display_name"] or None),
                # style 43 = group conversation, 45 = one-to-one.
                is_group=(r["chat_style"] == _GROUP_STYLE),
                service=r["service"],
                is_read=bool(r["is_read"]),
                reaction_verb=verb,
                reaction_target_guid=target if verb else None,
                reply_to_guid=r["thread_originator_guid"] or None,
                attachments=attachments.get(r["rowid"], []),
            ))
        return out
    finally:
        con.close()


def _read_participants(con: sqlite3.Connection) -> dict[int, list[str]]:
    """chat ROWID → member handles.

    Read once up front rather than per message: a full history is ~223k
    messages over a few hundred chats, so joining this into the message query
    would repeat every group's membership thousands of times.

    Apple's `chat_handle_join` lists the *other* members — our own handle
    isn't a row — which is already the shape `handles.group_key` wants.
    Missing on old schemas, where groups simply keep their backup-local guid.
    """
    out: dict[int, list[str]] = {}
    try:
        rows = con.execute("""
            SELECT chj.chat_id AS chat_id, h.id AS handle
            FROM chat_handle_join chj
            JOIN handle h ON h.ROWID = chj.handle_id
        """)
    except sqlite3.Error:
        return out
    for r in rows:
        if r["handle"]:
            out.setdefault(r["chat_id"], []).append(r["handle"])
    return out


def _read_attachments(con: sqlite3.Connection) -> dict[int, list[dict]]:
    """message ROWID → [{filename, mime_type, transfer_name, total_bytes}]."""
    if not _columns(con, "attachment"):
        return {}
    out: dict[int, list[dict]] = {}
    try:
        rows = con.execute("""
            SELECT maj.message_id AS message_id,
                   a.filename     AS filename,
                   a.mime_type    AS mime_type,
                   a.transfer_name AS transfer_name,
                   a.total_bytes  AS total_bytes,
                   a.is_sticker   AS is_sticker
            FROM attachment a
            JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        """)
    except sqlite3.Error as e:
        log.warning("could not read attachments: %s", e)
        return {}
    for r in rows:
        out.setdefault(r["message_id"], []).append({
            "filename": r["filename"],
            "mime_type": r["mime_type"],
            "transfer_name": r["transfer_name"],
            "total_bytes": r["total_bytes"],
            # Stickers are drawn bare (transparent, no bubble), unlike a
            # regular image attachment.
            "is_sticker": bool(r["is_sticker"]),
        })
    return out

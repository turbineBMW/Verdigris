"""Read-only checklist decoder for the disposable-note experiment.

Field definitions: https://github.com/threeplanetssoftware/apple_cloud_notes_parser/blob/master/proto/notestore.proto
No database mutation, fallback to HTML, or partial-success parsing.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re
import sqlite3
import gzip

MAX_BYTES = 8 * 1024 * 1024
FIXTURE_PREFIX = "Verdigris Checklist Probe "


def fields(data: bytes) -> dict[int, list[int | bytes]]:
    pos = 0

    def varint() -> int:
        nonlocal pos
        result = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("Truncated protobuf varint")
            value = data[pos]
            pos += 1
            if shift == 63 and value > 1:
                raise ValueError("Protobuf varint exceeds 64 bits")
            result |= (value & 127) << shift
            if not value & 128:
                return result
        raise ValueError("Invalid protobuf varint")

    result: dict[int, list[int | bytes]] = {}
    while pos < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Invalid protobuf field zero")
        if wire == 0:
            value: int | bytes = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else {1: 8, 5: 4}[wire]
            if length > len(data) - pos:
                raise ValueError("Truncated protobuf field")
            value = data[pos:pos + length]
            pos += length
        else:
            raise ValueError("Unsupported protobuf wire type")
        result.setdefault(number, []).append(value)
    return result


def one(message: dict, number: int, kind: type, default=None):
    values = message.get(number, [])
    if not values and default is not None:
        return default
    if len(values) != 1 or not isinstance(values[0], kind):
        raise ValueError(f"Missing or ambiguous protobuf field {number}")
    return values[0]


def decode(blob: bytes) -> dict:
    if len(blob) > MAX_BYTES:
        raise ValueError("Note exceeds prototype size limit")
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as zipped:
        raw = zipped.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Decompressed note exceeds prototype size limit")
    wrapper = fields(one(fields(raw), 2, bytes))
    note = fields(one(wrapper, 3, bytes))
    text = one(note, 2, bytes).decode("utf-8")
    encoded = text.encode("utf-16-le")
    paragraphs = []
    offset = 0
    for line in text.splitlines(keepends=True):
        size = len(line.encode("utf-16-le")) // 2
        paragraphs.append((offset, offset + size, line.rstrip("\r\n")))
        offset += size
    items: dict[int, dict] = {}
    offset = 0
    attachments = []
    for raw_run in note.get(5, []):
        if not isinstance(raw_run, bytes):
            raise ValueError("Invalid attribute run")
        run = fields(raw_run)
        length = one(run, 1, int)
        end = offset + length
        if end * 2 > len(encoded):
            raise ValueError("Attribute run exceeds text")
        if 12 in run:
            attachments.append(one(run, 12, bytes).hex())
        if 2 in run:
            style = fields(one(run, 2, bytes))
            if one(style, 1, int, -1) == 103:
                checklist = fields(one(style, 5, bytes))
                identifier = one(checklist, 1, bytes)
                done = one(checklist, 2, int)
                if not identifier or done not in (0, 1):
                    raise ValueError("Invalid checklist identifier or state")
                for start, stop, label in paragraphs:
                    if start < end and stop > offset:
                        item = {"id": identifier.hex(), "checked": bool(done), "text": label,
                                "location": start, "length": len(label.encode("utf-16-le")) // 2,
                                "indent": one(style, 4, int, 0)}
                        if start in items and items[start] != item:
                            raise ValueError("Conflicting checklist runs in one paragraph")
                        items[start] = item
        offset = end
    if offset * 2 != len(encoded):
        raise ValueError("Attribute runs do not cover the full text")
    ids = [item["id"] for item in items.values()]
    if len(set(ids)) != len(ids):
        raise ValueError("Checklist identifier spans multiple paragraphs; targeting is ambiguous")
    return {"text": text, "revision": hashlib.sha256(blob).hexdigest(),
            "items": list(items.values()), "attachmentRuns": attachments}


def read_fixture(note_id: str, database: Path | None = None) -> dict:
    match = re.fullmatch(r"x-coredata://[^/]+/ICNote/p([0-9]+)", note_id)
    if not match:
        raise ValueError("Invalid Notes identifier")
    database = database or Path.home() / "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
    # A live SQLite read transaction observes the WAL consistently. Never copy only
    # the .sqlite file or modify Core Data / CloudKit bookkeeping directly.
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT n.ZTITLE1, n.ZISPASSWORDPROTECTED, d.ZDATA FROM ZICCLOUDSYNCINGOBJECT n "
            "JOIN ZICNOTEDATA d ON d.ZNOTE=n.Z_PK WHERE n.Z_PK=?", (int(match[1]),)
        ).fetchone()
    if row is None:
        raise ValueError("Fixture note is not stored yet")
    title, protected, blob = row
    if protected or not isinstance(title, str) or not title.startswith(FIXTURE_PREFIX):
        raise ValueError("Only unprotected disposable fixture notes may be read")
    result = decode(blob)
    if not result["text"].startswith(title + "\n"):
        raise ValueError("Fixture title and document disagree")
    return {"noteId": note_id, **result}

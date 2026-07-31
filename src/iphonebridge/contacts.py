"""Contacts cache — pull vCards from iPhone via PBAP, store in SQLite,
resolve phone numbers to display names.

PBAP API quirk (per spike/RESULTS.md §4): use `Select(location, phonebook)`
not `SetFolder`. Then `PullAll(targetfile, filters)`.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

import dbus

from iphonebridge import config
from iphonebridge.bus import obex
from iphonebridge.events import normalize_phone
from iphonebridge.obex.sessions import SessionManager

log = logging.getLogger(__name__)


# ---- vCard parsing ------------------------------------------------------

_VCARD_BLOCK = re.compile(
    r"BEGIN:VCARD(?P<body>.*?)END:VCARD", re.DOTALL | re.IGNORECASE
)


def _unfold(body: str) -> list[str]:
    """RFC 6350 line unfolding: a line starting with a space or tab is a
    continuation of the previous logical line (vCard PHOTO payloads are
    almost always folded this way)."""
    lines: list[str] = []
    for raw in body.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_vcards(
    blob: str,
) -> list[tuple[str | None, str | None, list[str], bytes | None]]:
    """Return [(full_name, nickname, [phone_norm, ...], photo_bytes), ...].

    A nickname is what you'd actually call someone, so when the contact has
    one it's the better display name — same as Messages on iOS.
    """
    out: list[tuple[str | None, str | None, list[str], bytes | None]] = []
    for m in _VCARD_BLOCK.finditer(blob):
        fn: str | None = None
        nickname: str | None = None
        phones: list[str] = []
        photo_b64: str | None = None
        for line in _unfold(m.group("body")):
            line = line.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("FN:"):
                fn = line[3:].strip() or None
            elif upper.startswith("NICKNAME"):
                # NICKNAME:Chris  /  NICKNAME;CHARSET=UTF-8:Chris
                _, _, val = line.partition(":")
                # vCard allows a comma-separated list; take the first.
                nickname = val.split(",")[0].strip() or None
            elif upper.startswith("TEL"):
                # forms: TEL:1234, TEL;TYPE=CELL:1234, TEL;TYPE=CELL,VOICE:1234
                _, _, val = line.partition(":")
                norm = normalize_phone(val)
                if norm:
                    phones.append(norm)
            elif upper.startswith("PHOTO"):
                # forms: PHOTO;ENCODING=b;TYPE=JPEG:<base64>,
                #        PHOTO;ENCODING=BASE64;TYPE=JPEG:<base64>
                params, _, val = line.partition(":")
                if "B" in params.upper():  # ENCODING=b / BASE64
                    photo_b64 = val.strip()
        photo: bytes | None = None
        if photo_b64:
            try:
                photo = base64.b64decode(photo_b64, validate=False)
            except (binascii.Error, ValueError):
                photo = None
        if fn or phones:
            out.append((fn, nickname, phones, photo))
    return out


# ---- SQLite schema ------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name    TEXT NOT NULL,
    nickname     TEXT,
    photo_path   TEXT,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phones (
    phone_norm   TEXT NOT NULL,
    contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    UNIQUE(phone_norm, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_phones_norm ON phones(phone_norm);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def _open_db() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.CONTACTS_DB)
    conn.executescript(_SCHEMA)
    # Migrate DBs created before these columns existed.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(contacts)")}
    for col, ddl in (("photo_path", "TEXT"), ("nickname", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ddl}")
            conn.commit()
    return conn


# ---- PBAP pull ----------------------------------------------------------

def pull_phonebook(sessions: SessionManager, *, max_contacts: int = 65535) -> int:
    """Pull the iPhone's main phonebook over PBAP and return contact count.

    Replaces the local cache atomically (transaction).
    """
    pbap = obex(sessions.pbap_path, "org.bluez.obex.PhonebookAccess1")
    log.info("PBAP Select(int, pb)")
    pbap.Select("int", "pb")

    out = Path(tempfile.mkdtemp(prefix="iphonebridge_pb_")) / "pb.vcf"
    log.info("PBAP PullAll → %s (max=%d)", out, max_contacts)
    # Ask for NICKNAME explicitly. Without a Fields filter the iPhone
    # returns only its default subset (name/phone/photo), so nicknames were
    # never in the vCards to begin with — the parser had nothing to find.
    fields = dbus.Array(
        ["VERSION", "FN", "N", "NICKNAME", "TEL", "EMAIL", "PHOTO"],
        signature="s")
    ret = pbap.PullAll(
        str(out),
        {"MaxListCount": dbus.UInt16(max_contacts),
         "Format": dbus.String("Vcard30"),
         "Fields": fields},
    )
    transfer_path = str(ret[0]) if isinstance(ret, (tuple, list)) else str(ret)

    # Wait for transfer to complete (poll properties)
    tprops = obex(transfer_path, "org.freedesktop.DBus.Properties")
    for _ in range(600):  # up to 60s for huge phonebooks
        try:
            status = str(tprops.Get("org.bluez.obex.Transfer1", "Status"))
        except dbus.exceptions.DBusException:
            status = "gone"
            break
        if status in ("complete", "error"):
            break
        time.sleep(0.1)
    log.info("transfer status: %s, file size: %d bytes",
             status, out.stat().st_size if out.exists() else 0)

    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("PBAP transfer wrote no file")

    blob = out.read_text(errors="replace")
    parsed = _parse_vcards(blob)
    photo_count = sum(1 for _fn, _nick, _ph, photo in parsed if photo)
    nick_count = sum(1 for _fn, nick, _ph, _p in parsed if nick)
    log.info("parsed %d contacts (%d with photos, %d with nicknames) "
             "from %d bytes",
             len(parsed), photo_count, nick_count, out.stat().st_size)

    # Full resync — old photo files are keyed by contact id, which never
    # gets reused (AUTOINCREMENT), so clear the directory first or they'd
    # just accumulate as orphans forever.
    config.ensure_dirs()
    for stale in config.PHOTOS_DIR.glob("*.jpg"):
        try:
            stale.unlink()
        except OSError:
            pass

    now = time.time()
    with closing(_open_db()) as db:
        with db:  # transaction
            db.execute("DELETE FROM contacts")
            db.execute("DELETE FROM phones")
            for fn, nickname, phones, photo in parsed:
                if not fn and not phones:
                    continue
                cur = db.execute(
                    "INSERT INTO contacts(full_name, nickname, updated_at) "
                    "VALUES (?, ?, ?)",
                    (fn or "", nickname, now),
                )
                cid = cur.lastrowid
                photo_path = None
                if photo:
                    photo_path = str(config.PHOTOS_DIR / f"{cid}.jpg")
                    try:
                        Path(photo_path).write_bytes(photo)
                    except OSError:
                        log.warning("failed to write photo for contact %d", cid)
                        photo_path = None
                if photo_path:
                    db.execute(
                        "UPDATE contacts SET photo_path = ? WHERE id = ?",
                        (photo_path, cid),
                    )
                for p in phones:
                    db.execute(
                        "INSERT OR IGNORE INTO phones(phone_norm, contact_id) "
                        "VALUES (?, ?)",
                        (p, cid),
                    )
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('last_pull', ?), ('count', ?)",
                (str(now), str(len(parsed))),
            )

    # Clean up the temp file
    try:
        out.unlink()
        out.parent.rmdir()
    except OSError:
        pass

    return len(parsed)


# ---- Lookup -------------------------------------------------------------

class ContactsResolver:
    """In-process cache + SQLite-backed resolver. Cheap to construct.

    The instance is the stable handle held by event listeners — call
    `refresh()` to reload from disk in place, don't replace the object,
    otherwise bound `resolve` methods become stale.
    """

    def __init__(self) -> None:
        self._mem: dict[str, str] = {}
        self._photos: dict[str, str] = {}
        self._warm()

    def _warm(self) -> None:
        try:
            with closing(_open_db()) as db:
                for phone, name, photo_path in db.execute(
                    # A nickname wins over the formal name when one is set,
                    # which is what Messages shows on the phone.
                    "SELECT p.phone_norm, "
                    "       COALESCE(NULLIF(TRIM(c.nickname), ''), c.full_name), "
                    "       c.photo_path "
                    "FROM phones p JOIN contacts c ON c.id = p.contact_id "
                    "WHERE c.full_name != '' OR TRIM(COALESCE(c.nickname,'')) != ''"
                ):
                    self._mem[phone] = name
                    if photo_path:
                        self._photos[phone] = photo_path
        except sqlite3.Error as e:
            log.warning("contacts cache warm failed: %s", e)

    def refresh(self) -> int:
        """Re-read the SQLite cache into memory. Returns new count."""
        self._mem.clear()
        self._photos.clear()
        self._warm()
        return len(self._mem)

    def find_by_name(self, query: str) -> list[tuple[str, str]]:
        """Reverse lookup — name substring → list of (display_name, phone).

        Case-insensitive substring match against full_name. Returns all
        phones for each matching contact, deduplicated. Empty list if
        no matches.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        seen: set[tuple[str, str]] = set()
        try:
            with closing(_open_db()) as db:
                cur = db.execute(
                    "SELECT c.full_name, p.phone_norm "
                    "FROM contacts c JOIN phones p ON p.contact_id = c.id "
                    "WHERE c.full_name != '' "
                    "  AND LOWER(c.full_name) LIKE ? "
                    "ORDER BY c.full_name",
                    (f"%{q}%",),
                )
                for name, phone in cur:
                    key = (name, phone)
                    if key in seen:
                        continue
                    seen.add(key)
        except sqlite3.Error as e:
            log.warning("contacts find_by_name failed: %s", e)
            return []
        return list(seen)

    @staticmethod
    def _lookup(mapping: dict[str, str], norm: str) -> str | None:
        # Match exact, or suffix-match (US numbers might be stored 10 vs 11 digit
        # depending on whether the country code +1 was included). Match in BOTH
        # directions: a 10-digit incoming might match an 11-digit stored, and
        # vice versa.
        if norm in mapping:
            return mapping[norm]
        if len(norm) >= 10:
            tail = norm[-10:]
            for k, v in mapping.items():
                if k.endswith(tail):
                    return v
        return None

    def resolve(self, raw: str | None) -> str | None:
        norm = normalize_phone(raw)
        if not norm:
            return None
        return self._lookup(self._mem, norm)

    def resolve_photo(self, raw: str | None) -> str | None:
        """Path to a contact's cached PBAP photo, or None if it has none."""
        norm = normalize_phone(raw)
        if not norm:
            return None
        return self._lookup(self._photos, norm)

    def count(self) -> int:
        return len(self._mem)

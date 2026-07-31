"""SQLite message event store with FTS search and thread paging.

Durable history for the daemon, backup-sync, UI, and CLI. Layout:

  events   — every sms_* / message_state row (JSON payload + denorm columns)
  threads  — summary rows for the conversation list (no full message load)
  messages_fts — FTS5 index over message bodies for fast search

Handles are unique (`guid:…`, MAP transfer ids, `state:…`). Upsert by handle
keeps re-syncs idempotent.
"""
from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from iphonebridge import config
from iphonebridge.events import normalize_phone

log = logging.getLogger(__name__)

# Base tables only — indexes that reference denorm columns are created
# after _ensure_columns, so a DB created before those columns existed can
# still open (CREATE TABLE IF NOT EXISTS will not add columns).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL,
    handle        TEXT    NOT NULL UNIQUE,
    guid          TEXT,
    ts            TEXT,
    source        TEXT    NOT NULL DEFAULT 'live',
    payload       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    key           TEXT PRIMARY KEY,
    name          TEXT,
    phone         TEXT,
    is_group      INTEGER NOT NULL DEFAULT 0,
    last_ts       TEXT,
    last_preview  TEXT,
    last_event_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_guid ON events(guid);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_thread_ts ON events(thread_key, id);
CREATE INDEX IF NOT EXISTS idx_events_thread_msg
    ON events(thread_key, id) WHERE kind IN ('sms_received', 'sms_sent');
CREATE INDEX IF NOT EXISTS idx_events_reaction_target
    ON events(reaction_target) WHERE is_reaction = 1;
CREATE INDEX IF NOT EXISTS idx_threads_last_ts ON threads(last_ts);
"""

# Standalone FTS index (not content=events). External-content FTS5 plus the
# 'delete' command was producing "database disk image is malformed" on
# upsert in our environment; keeping the index as its own table is simpler
# and still sub-millisecond for prefix search.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    body,
    contact_name,
    tokenize='unicode61 remove_diacritics 2'
);
"""

MESSAGE_KINDS = frozenset({"sms_received", "sms_sent"})
STATE_KINDS = frozenset({"message_state"})
HISTORY_KINDS = MESSAGE_KINDS | STATE_KINDS

# How many messages to load when opening a conversation (newest first).
DEFAULT_MESSAGE_PAGE = 60
# Search result cap — enough to scroll, cheap for FTS.
DEFAULT_SEARCH_LIMIT = 80

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
# FTS snippet markers → HTML for the QML RichText preview.
_SNIP_OPEN = "\ue000"
_SNIP_CLOSE = "\ue001"


def thread_key_for(ev: dict) -> str:
    """Stable conversation key shared by the store and the UI.

    Groups key on membership / chat id. 1:1 chats key on normalized phone
    (`tel:…`) so MAP name changes never split a person in two. Fallback is
    a display-name key when no phone is present (email handles, etc.).
    """
    # Live iMessage puts the participant-set key on `chat_guid`; the backup
    # puts the same string on `group_key` and a phone-local id on `chat_guid`.
    # Prefer the imessage-group: form whenever either field has it.
    for field in ("group_key", "chat_guid"):
        val = ev.get(field)
        if isinstance(val, str) and val.startswith("imessage-group:"):
            return val
    group = ev.get("group_key") or ev.get("chat_guid")
    if group:
        return str(group)
    norm = ev.get("sender_phone_norm") or normalize_phone(
        ev.get("sender_phone") or ""
    )
    if norm:
        return f"tel:{norm}"
    name = (ev.get("contact_name") or ev.get("sender_phone")
            or ev.get("peer_handle") or "").strip()
    if name:
        # Strip accidental mailto:/tel: for a stable name key.
        if name.startswith("mailto:"):
            name = name[7:]
        elif name.startswith("tel:"):
            name = name[4:]
        return f"name:{name}"
    handle = ev.get("handle") or ""
    return f"handle:{handle}" if handle else "unknown"


# Apple message UUID, optionally with a live-session "-YYYYMMDDHHMMSSssssss"
# suffix the desktop used to append (transfer/send paths).
_GUID_RE = re.compile(
    r"^([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})(?:-\d{10,})?$"
)


def extract_guid(value: str | None) -> str | None:
    """Pull a bare UUID out of a handle/guid field, if present."""
    if not value:
        return None
    s = value.strip()
    if s.startswith("guid:"):
        s = s[5:]
    m = _GUID_RE.match(s)
    return m.group(1) if m else None


def _path_attachments(atts) -> list[dict]:
    """Attachment descriptors that actually have bytes on disk."""
    if not isinstance(atts, list):
        return []
    return [
        a for a in atts
        if isinstance(a, dict) and a.get("path")
    ]


def _is_filename_placeholder(body: str, atts: list | None = None) -> bool:
    """True for the bridge's `[name.ext]` stand-in for an undownloaded image.

    Live iMessage delivers attachments by reference; until the bytes land
    (or backup supplies a path) the body is just that bracketed filename.
    """
    if not body or body[0] != "[" or body[-1] != "]":
        return False
    if atts:
        names = {
            f"[{a.get('name')}]"
            for a in atts
            if isinstance(a, dict) and a.get("name")
        }
        if body in names:
            return True
    # `[IMG_0103.jpeg]`, `[uuid.png]` — has a dot so it's a filename, not
    # e.g. a rare one-word message someone put in brackets.
    inner = body[1:-1]
    return bool(inner) and "." in inner and "\n" not in inner and len(inner) < 200


def merge_message_payload(existing: dict, incoming: dict) -> dict:
    """Combine two sms_* payloads for the same handle without losing media.

    Live redelivery often rewrites the row with only `[name.png]` and no
    `attachments` — that used to wipe a richer backup (or earlier live
    download) and leave the UI stuck on the placeholder forever.
    """
    out = dict(incoming)
    old_paths = _path_attachments(existing.get("attachments"))
    new_paths = _path_attachments(incoming.get("attachments"))
    if old_paths and not new_paths:
        out["attachments"] = list(existing.get("attachments") or old_paths)
    elif new_paths:
        out["attachments"] = list(incoming.get("attachments") or new_paths)
    elif existing.get("attachments") and not incoming.get("attachments"):
        out["attachments"] = list(existing["attachments"])

    atts = out.get("attachments") or []
    new_body = out.get("body") or ""
    old_body = existing.get("body") or ""
    if _path_attachments(atts) and _is_filename_placeholder(new_body, atts):
        # Bytes are here — the bracketed name is the caption it replaced.
        out["body"] = ""
    elif not new_body and old_body and not _is_filename_placeholder(old_body, atts):
        # Backup attachment-only rows have empty text; keep a real live caption.
        out["body"] = old_body
    elif (
        _is_filename_placeholder(new_body, atts)
        and old_body
        and not _is_filename_placeholder(old_body, atts)
        and not _path_attachments(atts)
    ):
        # Live placeholder over a real caption (no media yet): keep the text.
        out["body"] = old_body

    # Prefer non-empty routing / identity fields from either side.
    for key in (
        "chat_guid", "group_key", "chat_name", "contact_name", "sender_name",
        "sender_phone", "sender_phone_norm", "reply_to_guid", "guid",
    ):
        if not out.get(key) and existing.get(key):
            out[key] = existing[key]
    return out


def stable_handle(payload: dict) -> str:
    """Canonical event handle so live + backup upsert the same row.

    Forms we collapse to `guid:{uuid}`:
      - backup: `guid:UUID`
      - live iMessage: bare `UUID`
      - older live sends: `UUID-20260730213831119063`

    `message_state` rows keep synthetic `state:…` handles — their `guid`
    field names the *target* message, not their own identity.
    """
    kind = payload.get("kind") or ""
    handle = (payload.get("handle") or "").strip()
    if kind == "message_state":
        return handle or (
            f"state:{(payload.get('guid') or '')}:"
            f"{payload.get('state') or ''}:"
            f"{payload.get('timestamp') or ''}"
        )
    # Explicit guid field wins; else peel UUID out of the handle.
    guid = extract_guid(payload.get("guid")) or extract_guid(handle)
    if guid and kind in ("sms_received", "sms_sent", ""):
        return f"guid:{guid}"
    if handle:
        return handle
    return f"auto:{uuid.uuid4().hex}"


def _ts_epoch(ts: str | None) -> float:
    """Parse ISO timestamps to a comparable epoch; 0 if unknown."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime

        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _is_reaction_payload(payload: dict) -> bool:
    return bool(payload.get("reaction_verb") or payload.get("im_reaction_verb"))


def _denorm_fields(
    payload: dict,
) -> tuple[str, str, str, int, int, str | None, float]:
    """body, thread_key, contact_name, outgoing, is_reaction, reaction_target, ts_epoch."""
    kind = payload.get("kind") or ""
    body = payload.get("body") or ""
    ts = payload.get("timestamp") or payload.get("seen_at") or None
    epoch = _ts_epoch(ts if isinstance(ts, str) else None)
    if kind == "message_state":
        peer = {
            "sender_phone": payload.get("peer_handle") or "",
            "sender_phone_norm": normalize_phone(
                payload.get("peer_handle") or ""
            ),
        }
        tkey = thread_key_for(peer) if peer.get("sender_phone") else ""
        return body, tkey, "", 0, 0, None, epoch

    is_react = 1 if _is_reaction_payload(payload) else 0
    target = (
        payload.get("reaction_target_guid")
        or payload.get("im_reaction_target_guid")
        or None
    )
    outgoing = 1 if kind == "sms_sent" else 0
    # Do NOT put chat_name into contact_name — that made group rows look
    # like they were from a person named after the group.
    contact = (
        payload.get("contact_name")
        or payload.get("sender_name")
        or ""
    )
    tkey = thread_key_for(payload)
    return body, tkey, contact, outgoing, is_react, target, epoch


def _default_path() -> Path:
    return config.MESSAGES_DB


def _fts_match_query(user_query: str) -> str:
    """Turn free text into an FTS5 MATCH expression (AND of prefix tokens)."""
    tokens = _WORD_RE.findall(user_query.strip())
    if not tokens:
        return ""
    parts = []
    for t in tokens:
        # Escape embedded double-quotes for FTS string literals.
        safe = t.replace('"', '""')
        parts.append(f'"{safe}"*')
    return " AND ".join(parts)


def _snippet_to_html(snippet: str) -> str:
    """Escape HTML and turn FTS markers into <b> highlights."""
    if not snippet:
        return ""
    # Split on markers so only the match spans become <b>, rest is escaped.
    out: list[str] = []
    i = 0
    while i < len(snippet):
        if snippet.startswith(_SNIP_OPEN, i):
            j = snippet.find(_SNIP_CLOSE, i + len(_SNIP_OPEN))
            if j < 0:
                out.append(html.escape(snippet[i + len(_SNIP_OPEN):]))
                break
            out.append("<b>")
            out.append(html.escape(
                snippet[i + len(_SNIP_OPEN):j]
            ))
            out.append("</b>")
            i = j + len(_SNIP_CLOSE)
        else:
            n = snippet.find(_SNIP_OPEN, i)
            if n < 0:
                out.append(html.escape(snippet[i:]))
                break
            out.append(html.escape(snippet[i:n]))
            i = n
    return "".join(out).replace("\n", " ")


class MessageStore:
    """Process-local handle on the messages DB."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ---- connection lifecycle -------------------------------------------

    def open(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            config.ensure_dirs()
            conn = sqlite3.connect(
                self.path,
                timeout=60,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.executescript(_SCHEMA)
            self._ensure_columns(conn)
            conn.executescript(_INDEXES)
            try:
                conn.executescript(_FTS_SCHEMA)
            except sqlite3.OperationalError as e:
                log.warning("FTS5 unavailable (%s) — search will be LIKE-based", e)
            self._conn = conn
            self._migrate_jsonl_once(conn)
            self._ensure_denorm(conn)
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.open()
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        for name, decl in (
            ("body", "TEXT"),
            ("thread_key", "TEXT"),
            ("contact_name", "TEXT"),
            ("outgoing", "INTEGER NOT NULL DEFAULT 0"),
            ("is_reaction", "INTEGER NOT NULL DEFAULT 0"),
            ("reaction_target", "TEXT"),
            ("ts_epoch", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
        tcols = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if "last_ts_epoch" not in tcols:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN last_ts_epoch REAL NOT NULL DEFAULT 0"
            )
        conn.commit()

    # ---- row packing ----------------------------------------------------

    def _pack(self, payload: dict, *, source: str) -> tuple:
        """Full events-table row for INSERT (without id)."""
        kind = payload.get("kind") or ""
        handle = stable_handle(payload)
        # Keep payload handle in sync so reloads dedupe consistently.
        if payload.get("handle") != handle:
            payload = {**payload, "handle": handle}
        guid = payload.get("guid") or None
        ts = payload.get("timestamp") or payload.get("seen_at") or None
        if ts is not None and not isinstance(ts, str):
            ts = str(ts)
        body, tkey, contact, outgoing, is_react, rtarget, epoch = _denorm_fields(
            payload
        )
        blob = json.dumps(payload, ensure_ascii=False)
        return (
            kind, handle, guid, ts, source, blob,
            body, tkey, contact, outgoing, is_react, rtarget, epoch,
        )

    _INSERT_SQL = (
        "INSERT INTO events "
        "(kind, handle, guid, ts, source, payload, "
        " body, thread_key, contact_name, outgoing, is_reaction, "
        " reaction_target, ts_epoch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(handle) DO UPDATE SET "
        "  kind=excluded.kind, guid=excluded.guid, ts=excluded.ts, "
        "  source=excluded.source, payload=excluded.payload, "
        "  body=excluded.body, thread_key=excluded.thread_key, "
        "  contact_name=excluded.contact_name, outgoing=excluded.outgoing, "
        "  is_reaction=excluded.is_reaction, "
        "  reaction_target=excluded.reaction_target, "
        "  ts_epoch=excluded.ts_epoch"
    )

    def _existing_payloads(
        self, conn: sqlite3.Connection, handles: list[str]
    ) -> dict[str, dict]:
        """Map handle → payload for handles that already exist."""
        out: dict[str, dict] = {}
        for i in range(0, len(handles), 500):
            chunk = [h for h in handles[i:i + 500] if h]
            if not chunk:
                continue
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT handle, payload FROM events WHERE handle IN ({ph})",
                chunk,
            ):
                try:
                    out[row["handle"]] = json.loads(row["payload"])
                except json.JSONDecodeError:
                    continue
        return out

    def _prepare_payload(self, payload: dict, existing: dict | None) -> dict:
        """Stable handle + merge with any prior row for the same message."""
        kind = payload.get("kind") or ""
        if (
            existing is not None
            and kind in MESSAGE_KINDS
            and (existing.get("kind") or "") in MESSAGE_KINDS
        ):
            return merge_message_payload(existing, payload)
        return payload

    def upsert_event(self, payload: dict, *, source: str = "live") -> int:
        with self._lock, self.connection() as conn:
            handle = stable_handle(payload)
            existing = self._existing_payloads(conn, [handle]).get(handle)
            payload = self._prepare_payload(payload, existing)
            row = self._pack(payload, source=source)
            conn.execute(self._INSERT_SQL, row)
            rid = conn.execute(
                "SELECT id FROM events WHERE handle = ?", (row[1],)
            ).fetchone()
            eid = int(rid["id"])
            self._fts_upsert(conn, eid, row[6] or "", row[8] or "")
            if row[0] in MESSAGE_KINDS and not row[10]:
                self._touch_thread(conn, row, eid)
            self._bump_write_seq(conn)
            conn.commit()
            return eid

    def upsert_many(
        self, events: Iterable[dict], *, source: str = "live"
    ) -> int:
        items = list(events)
        if not items:
            return 0
        with self._lock, self.connection() as conn:
            handles = [stable_handle(p) for p in items]
            existing = self._existing_payloads(conn, handles)
            batch = []
            for p, h in zip(items, handles):
                p = self._prepare_payload(p, existing.get(h))
                batch.append(self._pack(p, source=source))
            conn.executemany(self._INSERT_SQL, batch)
            for i in range(0, len(handles), 500):
                chunk = handles[i:i + 500]
                ph = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT id, handle, body, contact_name, kind, is_reaction "
                    f"FROM events WHERE handle IN ({ph})",
                    chunk,
                ):
                    if r["kind"] in MESSAGE_KINDS and not r["is_reaction"]:
                        self._fts_upsert(
                            conn, int(r["id"]), r["body"] or "",
                            r["contact_name"] or "",
                        )
            keys = {r[7] for r in batch if r[0] in MESSAGE_KINDS and r[7]}
            for key in keys:
                self._rebuild_thread(conn, key)
            self._bump_write_seq(conn)
            conn.commit()
        return len(batch)

    def _bump_write_seq(self, conn: sqlite3.Connection) -> int:
        """Monotonic counter so the UI can notice in-place upserts (low ids)."""
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'write_seq'"
        ).fetchone()
        n = int(row["value"] if row else 0) + 1
        self._meta_set(conn, "write_seq", str(n))
        return n

    def write_seq(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'write_seq'"
            ).fetchone()
            return int(row["value"] if row else 0)

    def upsert_state(self, props: dict) -> int | None:
        state = props.get("state") or ""
        if state in ("typing", "typing_stopped"):
            return None
        guid = props.get("guid") or ""
        ts = props.get("timestamp") or ""
        peer = props.get("handle") or ""
        handle = props.get("handle_id") or f"state:{guid}:{state}:{ts}"
        body = props.get("body") or ""
        payload = {
            "kind": "message_state",
            "handle": handle,
            "guid": guid,
            "state": state,
            "peer_handle": peer,
            "timestamp": ts,
            "body": body,
        }
        eid = self.upsert_event(payload, source="live")
        # Also fold path-bearing attachments into the message row itself so a
        # cold load (messages_page) shows the image even before state hydrate,
        # and so a later live placeholder rewrite has something to merge with.
        if state == "attachments" and guid and body:
            try:
                atts = json.loads(body)
            except json.JSONDecodeError:
                atts = None
            if isinstance(atts, list) and _path_attachments(atts):
                self._apply_attachments_to_message(guid, atts)
        return eid

    def _apply_attachments_to_message(
        self, guid: str, atts: list[dict]
    ) -> bool:
        """Write downloaded/backup attachment paths onto the sms_* row.

        Returns True when a message was updated.
        """
        with self._lock, self.connection() as conn:
            changed = self._patch_message_attachments_conn(conn, guid, atts)
            if changed:
                self._bump_write_seq(conn)
                conn.commit()
            return changed

    def _patch_message_attachments_conn(
        self,
        conn: sqlite3.Connection,
        guid: str,
        atts: list[dict],
        *,
        rebuild_thread: bool = True,
    ) -> bool:
        """Conn-level attachment fold. Caller owns the lock/commit."""
        bare = extract_guid(guid) or guid
        if not bare:
            return False
        path_atts = _path_attachments(atts)
        if not path_atts:
            return False
        row = conn.execute(
            "SELECT id, payload FROM events "
            "WHERE kind IN ('sms_received','sms_sent') "
            "AND (guid = ? OR handle = ?) LIMIT 1",
            (bare, f"guid:{bare}"),
        ).fetchone()
        if row is None:
            return False
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return False
        body = payload.get("body") or ""
        have_paths = _path_attachments(payload.get("attachments"))
        if have_paths:
            # Paths already on the message — only clear a leftover placeholder.
            if not _is_filename_placeholder(body, have_paths):
                return False
            payload["body"] = ""
        else:
            payload["attachments"] = path_atts
            if _is_filename_placeholder(body, path_atts):
                payload["body"] = ""
        body, tkey, contact, outgoing, is_react, rtarget, epoch = (
            _denorm_fields(payload)
        )
        conn.execute(
            "UPDATE events SET payload=?, body=?, thread_key=?, "
            "contact_name=?, outgoing=?, is_reaction=?, reaction_target=?, "
            "ts_epoch=? WHERE id=?",
            (
                json.dumps(payload, ensure_ascii=False),
                body, tkey, contact, outgoing, is_react, rtarget, epoch,
                int(row["id"]),
            ),
        )
        if not is_react:
            self._fts_upsert(conn, int(row["id"]), body, contact)
        if rebuild_thread and tkey and not is_react:
            self._rebuild_thread(conn, tkey)
        return True

    def _repair_attachment_paths(self, conn: sqlite3.Connection) -> int:
        """Restore media paths wiped by live placeholder rewrites.

        Sources, in order:
          1. `message_state` attachment announcements (live downloads)
          2. `backup_events.jsonl` rows that still carry extracted paths
        """
        repaired = 0
        # --- 1. fold state channel onto messages ---------------------------
        by_guid: dict[str, list[dict]] = {}
        for row in conn.execute(
            "SELECT guid, payload FROM events WHERE kind = 'message_state' "
            "ORDER BY id ASC"
        ):
            try:
                p = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if p.get("state") != "attachments":
                continue
            guid = extract_guid(p.get("guid") or row["guid"])
            if not guid:
                continue
            try:
                atts = json.loads(p.get("body") or "[]")
            except json.JSONDecodeError:
                continue
            paths = _path_attachments(atts if isinstance(atts, list) else [])
            if paths:
                by_guid[guid] = paths
        touched_threads: set[str] = set()
        for guid, atts in by_guid.items():
            if self._patch_message_attachments_conn(
                conn, guid, atts, rebuild_thread=False
            ):
                repaired += 1
                t = conn.execute(
                    "SELECT thread_key FROM events WHERE guid = ? "
                    "AND kind IN ('sms_received','sms_sent') LIMIT 1",
                    (guid,),
                ).fetchone()
                if t and t["thread_key"]:
                    touched_threads.add(t["thread_key"])

        # --- 2. backup_events.jsonl ----------------------------------------
        bak = Path(config.BACKUP_EVENTS_JSONL)
        if bak.is_file():
            try:
                with bak.open(encoding="utf-8") as fh:
                    for line in fh:
                        if '"path"' not in line or '"attachments"' not in line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        atts = _path_attachments(ev.get("attachments"))
                        if not atts:
                            continue
                        guid = extract_guid(
                            ev.get("guid") or ev.get("handle")
                        )
                        if not guid:
                            continue
                        if self._patch_message_attachments_conn(
                            conn, guid, atts, rebuild_thread=False
                        ):
                            repaired += 1
                            tkey = (
                                ev.get("group_key")
                                or ev.get("chat_guid")
                                or ""
                            )
                            row = conn.execute(
                                "SELECT thread_key FROM events "
                                "WHERE guid = ? AND kind IN "
                                "('sms_received','sms_sent') LIMIT 1",
                                (guid,),
                            ).fetchone()
                            if row and row["thread_key"]:
                                touched_threads.add(row["thread_key"])
                            elif tkey:
                                touched_threads.add(tkey)
            except OSError as e:
                log.warning("could not read %s for attachment repair: %s", bak, e)

        for key in touched_threads:
            self._rebuild_thread(conn, key)
        return repaired

    def _fts_upsert(
        self, conn: sqlite3.Connection, rowid: int, body: str, contact: str
    ) -> None:
        if not self._fts_ready(conn):
            return
        try:
            conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (rowid,))
            if body or contact:
                conn.execute(
                    "INSERT INTO messages_fts(rowid, body, contact_name) "
                    "VALUES (?, ?, ?)",
                    (rowid, body or "", contact or ""),
                )
        except sqlite3.Error as e:
            log.debug("fts upsert failed: %s", e)

    def _fts_ready(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("SELECT 1 FROM messages_fts LIMIT 0")
            return True
        except sqlite3.OperationalError:
            return False

    def _touch_thread(
        self, conn: sqlite3.Connection, row: tuple, event_id: int
    ) -> None:
        """Update threads summary if this event is newer *by time*."""
        # row: kind, handle, guid, ts, source, payload, body, tkey, contact,
        #      outgoing, is_react, rtarget, epoch
        kind = row[0]
        ts = row[3]
        payload_json = row[5]
        body = row[6]
        tkey = row[7]
        contact = row[8]
        is_react = row[10]
        epoch = float(row[12] or 0)
        if not tkey or is_react or kind not in MESSAGE_KINDS:
            return
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {}
        is_group = 1 if (
            payload.get("group_key")
            or payload.get("chat_guid")
            or payload.get("is_group")
            or str(tkey).startswith("imessage-group:")
        ) else 0
        # Groups keep the chat title; never the last sender's contact name.
        if is_group:
            name = (payload.get("chat_name") or "").strip() or "Group message"
            phone = None
        else:
            phone = payload.get("sender_phone") or payload.get("sender_phone_norm")
            name = contact or phone or tkey
        preview = (body or "").replace("\n", " ")[:200]
        existing = conn.execute(
            "SELECT last_event_id, last_ts_epoch FROM threads WHERE key = ?",
            (tkey,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO threads "
                "(key, name, phone, is_group, last_ts, last_preview, "
                " last_event_id, last_ts_epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tkey, name, phone, is_group, ts, preview, event_id, epoch),
            )
            return
        # Time wins — live upserts reuse low rowids, so id comparison was
        # freezing the sidebar on the last backup import forever.
        if epoch >= float(existing["last_ts_epoch"] or 0):
            if is_group:
                conn.execute(
                    "UPDATE threads SET name=?, is_group=1, phone=NULL, "
                    "last_ts=?, last_preview=?, last_event_id=?, "
                    "last_ts_epoch=? WHERE key=?",
                    (name, ts, preview, event_id, epoch, tkey),
                )
            else:
                conn.execute(
                    "UPDATE threads SET name=COALESCE(?, name), "
                    "phone=COALESCE(?, phone), is_group=0, last_ts=?, "
                    "last_preview=?, last_event_id=?, last_ts_epoch=? "
                    "WHERE key=?",
                    (name, phone, ts, preview, event_id, epoch, tkey),
                )

    def _rebuild_thread(self, conn: sqlite3.Connection, key: str) -> None:
        # Newest by wall-clock, not rowid — live traffic reuses low ids.
        row = conn.execute(
            "SELECT id, ts, body, contact_name, payload, ts_epoch FROM events "
            "WHERE thread_key = ? AND kind IN ('sms_received', 'sms_sent') "
            "AND is_reaction = 0 "
            "ORDER BY ts_epoch DESC, id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            conn.execute("DELETE FROM threads WHERE key = ?", (key,))
            return
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            payload = {}
        is_group = 1 if str(key).startswith("imessage-group:") else 0
        if is_group:
            name = (payload.get("chat_name") or "").strip() or "Group message"
            phone = None
        else:
            phone = payload.get("sender_phone") or payload.get("sender_phone_norm")
            name = row["contact_name"] or phone or key
        preview = (row["body"] or "").replace("\n", " ")[:200]
        epoch = float(row["ts_epoch"] or 0) or _ts_epoch(row["ts"])
        conn.execute(
            "INSERT INTO threads "
            "(key, name, phone, is_group, last_ts, last_preview, "
            " last_event_id, last_ts_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  name=excluded.name, phone=excluded.phone, "
            "  is_group=excluded.is_group, last_ts=excluded.last_ts, "
            "  last_preview=excluded.last_preview, "
            "  last_event_id=excluded.last_event_id, "
            "  last_ts_epoch=excluded.last_ts_epoch",
            (key, name, phone, is_group, row["ts"], preview,
             int(row["id"]), epoch),
        )

    # ---- reads: threads / paging / search --------------------------------

    def list_threads(self, *, limit: int | None = None) -> list[dict]:
        """Conversation summaries, newest activity first."""
        sql = (
            "SELECT key, name, phone, is_group, last_ts, last_preview, "
            "last_event_id, last_ts_epoch FROM threads "
            "ORDER BY last_ts_epoch DESC, last_event_id DESC"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "key": r["key"],
                "name": r["name"] or r["key"],
                "phone": r["phone"],
                "is_group": bool(r["is_group"]),
                "last_ts": r["last_ts"],
                "last_preview": r["last_preview"] or "",
                "last_event_id": int(r["last_event_id"] or 0),
                "last_ts_epoch": float(r["last_ts_epoch"] or 0),
            }
            for r in rows
        ]

    def count_unread(self, thread_key: str, after_ts: str | None) -> int:
        """Incoming non-reaction messages newer than the user's read mark.

        Compares on `ts_epoch`, never the ISO string. Events mix offsets
        (`-04:00` vs `+00:00`), so lexical `ts > ?` is not chronological —
        a message at 15:41 EDT stored as `…T19:41…+00:00` sorts *after* a
        read mark at 16:07 EDT stored as `…T16:07…-04:00`, and the unread
        badge resurrects after every restart (pinned tiles always use this
        path because their archive is not loaded).
        """
        clauses = [
            "thread_key = ?",
            "kind = 'sms_received'",
            "is_reaction = 0",
        ]
        params: list[object] = [thread_key]
        if after_ts:
            clauses.append("ts_epoch > ?")
            params.append(_ts_epoch(after_ts))
        sql = f"SELECT COUNT(*) AS c FROM events WHERE {' AND '.join(clauses)}"
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] if row else 0)

    def messages_page(
        self,
        thread_key: str,
        *,
        before_epoch: float | None = None,
        before_id: int | None = None,
        limit: int = DEFAULT_MESSAGE_PAGE,
    ) -> list[tuple[int, dict]]:
        """Newest `limit` messages for a thread, oldest-first.

        Ordered by **wall-clock** (`ts_epoch`), not rowid. Live upserts reuse
        low ids; ordering by id buried today's messages under the backup.

        Scroll-up: pass `before_epoch` (and optional `before_id` as a tie-break)
        of the oldest message already loaded.
        """
        clauses = [
            "thread_key = ?",
            "kind IN ('sms_received', 'sms_sent')",
        ]
        params: list[object] = [thread_key]
        if before_epoch is not None:
            # Strictly older than the window edge.
            clauses.append(
                "(ts_epoch < ? OR (ts_epoch = ? AND id < ?))"
            )
            params.extend([before_epoch, before_epoch, before_id or 0])
        where = " AND ".join(clauses)
        sql = (
            f"SELECT id, payload, ts_epoch FROM events WHERE {where} "
            f"ORDER BY ts_epoch DESC, id DESC LIMIT ?"
        )
        params.append(limit)
        with self.connection() as conn:
            rows = list(conn.execute(sql, params))
        out: list[tuple[int, dict]] = []
        for row in reversed(rows):
            try:
                ev = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            ev["_event_id"] = int(row["id"])
            ev["_ts_epoch"] = float(row["ts_epoch"] or 0)
            out.append((int(row["id"]), ev))
        return out

    def messages_around(
        self,
        thread_key: str,
        event_id: int,
        *,
        before: int = 40,
        after: int = 40,
    ) -> list[tuple[int, dict]]:
        """Window of messages centered on `event_id` (for jump-to-result).

        Uses the anchor row's stored `thread_key` when present so a search hit
        still loads even if the UI key drifted (tel: vs name: etc.).
        """
        with self.connection() as conn:
            anchor = conn.execute(
                "SELECT id, ts_epoch, thread_key FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if anchor is None:
            return self.messages_page(thread_key, limit=before + after)
        tkey = (anchor["thread_key"] or thread_key or "").strip() or thread_key
        epoch = float(anchor["ts_epoch"] or 0)
        # Strictly older than the hit.
        older = self.messages_page(
            tkey,
            before_epoch=epoch,
            before_id=event_id,
            limit=before,
        )
        # Anchor + newer by wall-clock (include the hit itself).
        with self.connection() as conn:
            newer_rows = list(conn.execute(
                "SELECT id, payload, ts_epoch FROM events "
                "WHERE thread_key = ? AND kind IN ('sms_received', 'sms_sent') "
                "AND (ts_epoch > ? OR (ts_epoch = ? AND id >= ?)) "
                "ORDER BY ts_epoch ASC, id ASC LIMIT ?",
                (tkey, epoch, epoch, event_id, after + 1),
            ))
        by_id: dict[int, tuple[int, dict]] = {}
        for eid, ev in older:
            by_id[eid] = (eid, ev)
        for row in newer_rows:
            try:
                ev = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            eid = int(row["id"])
            ev["_event_id"] = eid
            ev["_ts_epoch"] = float(row["ts_epoch"] or 0)
            by_id[eid] = (eid, ev)
        # Guarantee the search hit is in the window even if thread_key on the
        # page query missed it (empty denorm / race during rekey).
        if event_id not in by_id:
            with self.connection() as conn:
                row = conn.execute(
                    "SELECT id, payload, ts_epoch FROM events WHERE id = ?",
                    (event_id,),
                ).fetchone()
            if row is not None:
                try:
                    ev = json.loads(row["payload"])
                except json.JSONDecodeError:
                    ev = None
                if ev is not None:
                    eid = int(row["id"])
                    ev["_event_id"] = eid
                    ev["_ts_epoch"] = float(row["ts_epoch"] or 0)
                    by_id[eid] = (eid, ev)
        return [
            by_id[k]
            for k in sorted(
                by_id,
                key=lambda i: (
                    by_id[i][1].get("_ts_epoch") or 0,
                    i,
                ),
            )
        ]

    def states_for_guids(self, guids: Iterable[str]) -> list[dict]:
        guids = [g for g in guids if g]
        if not guids:
            return []
        out: list[dict] = []
        with self.connection() as conn:
            for i in range(0, len(guids), 400):
                chunk = guids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT payload FROM events WHERE kind = 'message_state' "
                    f"AND guid IN ({ph}) ORDER BY id ASC",
                    chunk,
                ):
                    try:
                        out.append(json.loads(row["payload"]))
                    except json.JSONDecodeError:
                        continue
        return out

    def has_older_messages(
        self,
        thread_key: str,
        oldest_epoch: float,
        oldest_id: int = 0,
    ) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE thread_key = ? "
                "AND kind IN ('sms_received', 'sms_sent') "
                "AND (ts_epoch < ? OR (ts_epoch = ? AND id < ?)) LIMIT 1",
                (thread_key, oldest_epoch, oldest_epoch, oldest_id),
            ).fetchone()
        return row is not None

    def search(
        self, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> list[dict]:
        """Full-text search over message bodies. One hit per matching message.

        Returns dicts ready for the sidebar search model:
          resultId, threadKey, name, preview (HTML), previewPlain, stamp,
          eventId, guid, ts, avatar fields filled by the UI.
        """
        q = (query or "").strip()
        if not q:
            return []
        match = _fts_match_query(q)
        with self.connection() as conn:
            if match and self._fts_ready(conn):
                try:
                    # Rank + limit inside FTS first, then join events. Highlight
                    # is done in Python (_manual_snippet) — FTS snippet() over
                    # a second join was an order of magnitude slower here.
                    rows = list(conn.execute(
                        "SELECT e.id, e.thread_key, e.body, e.ts, e.guid, "
                        "e.contact_name, e.payload, e.outgoing, "
                        "NULL AS snip "
                        "FROM ("
                        "  SELECT rowid AS rid FROM messages_fts "
                        "  WHERE messages_fts MATCH ? "
                        "  ORDER BY bm25(messages_fts) "
                        "  LIMIT ?"
                        ") hits "
                        "JOIN events e ON e.id = hits.rid "
                        "WHERE e.kind IN ('sms_received', 'sms_sent') "
                        "AND e.is_reaction = 0 "
                        "ORDER BY e.id DESC "
                        "LIMIT ?",
                        (match, limit * 2, limit),
                    ))
                except sqlite3.OperationalError as e:
                    log.debug("FTS search failed (%s); falling back to LIKE", e)
                    rows = self._search_like(conn, q, limit)
            else:
                rows = self._search_like(conn, q, limit)

        # Thread display names from the threads table (one query).
        keys = {r["thread_key"] for r in rows if r["thread_key"]}
        names: dict[str, str] = {}
        phones: dict[str, str] = {}
        if keys:
            with self.connection() as conn:
                ph = ",".join("?" * len(keys))
                for r in conn.execute(
                    f"SELECT key, name, phone FROM threads WHERE key IN ({ph})",
                    list(keys),
                ):
                    names[r["key"]] = r["name"] or r["key"]
                    phones[r["key"]] = r["phone"] or ""

        results: list[dict] = []
        for r in rows:
            tkey = r["thread_key"] or ""
            snip = r["snip"] if "snip" in r.keys() else None
            body_text = r["body"] or ""
            # FTS snippet() sometimes returns the bare body (no markers) when
            # the match is the whole field or the join path confuses it —
            # fall back to a manual highlight so the UI always gets <b>.
            if not snip or (_SNIP_OPEN not in snip and _SNIP_CLOSE not in snip):
                snip = self._manual_snippet(body_text, q)
            plain = body_text.replace("\n", " ")
            try:
                payload = json.loads(r["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            name = (
                names.get(tkey)
                or r["contact_name"]
                or payload.get("chat_name")
                or payload.get("sender_phone")
                or tkey
            )
            results.append({
                "resultId": str(r["id"]),
                "threadKey": tkey,
                "name": name,
                "phone": phones.get(tkey) or payload.get("sender_phone") or "",
                "preview": _snippet_to_html(snip or plain[:120]),
                "previewPlain": plain[:200],
                "stamp": r["ts"] or "",
                "eventId": int(r["id"]),
                "guid": r["guid"] or payload.get("guid") or "",
                "outgoing": bool(r["outgoing"]),
            })
        return results

    def _search_like(
        self, conn: sqlite3.Connection, query: str, limit: int
    ) -> list[sqlite3.Row]:
        """Fallback when FTS is missing or the query can't be parsed."""
        like = f"%{query}%"
        return list(conn.execute(
            "SELECT id, thread_key, body, ts, guid, contact_name, payload, "
            "outgoing, NULL AS snip FROM events "
            "WHERE kind IN ('sms_received', 'sms_sent') AND is_reaction = 0 "
            "AND body LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT ?",
            (like, limit),
        ))

    @staticmethod
    def _manual_snippet(body: str, query: str, radius: int = 40) -> str:
        low = body.lower()
        q = query.lower().strip()
        # First token hit.
        tokens = _WORD_RE.findall(q)
        pos = -1
        hit = ""
        for t in tokens or [q]:
            pos = low.find(t.lower())
            if pos >= 0:
                hit = body[pos:pos + len(t)]
                break
        if pos < 0:
            return body[: radius * 2].replace("\n", " ")
        start = max(0, pos - radius)
        end = min(len(body), pos + len(hit) + radius)
        frag = body[start:end].replace("\n", " ")
        # Re-find hit inside frag for markers.
        i = frag.lower().find(hit.lower())
        if i < 0:
            return frag
        marked = (
            frag[:i] + _SNIP_OPEN + frag[i:i + len(hit)] + _SNIP_CLOSE
            + frag[i + len(hit):]
        )
        if start > 0:
            marked = "…" + marked
        if end < len(body):
            marked = marked + "…"
        return marked

    # ---- generic event iteration (tests / CLI / seed) --------------------

    def max_id(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM events"
            ).fetchone()
            return int(row["m"] if row is not None else 0)

    def count(self, kinds: set[str] | None = None) -> int:
        with self.connection() as conn:
            if kinds:
                placeholders = ",".join("?" * len(kinds))
                row = conn.execute(
                    f"SELECT COUNT(*) AS c FROM events WHERE kind IN ({placeholders})",
                    tuple(kinds),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
            return int(row["c"] if row is not None else 0)

    def iter_events(
        self,
        *,
        kinds: set[str] | None = None,
        after_id: int = 0,
        limit: int | None = None,
    ) -> Iterator[tuple[int, dict]]:
        clauses: list[str] = []
        params: list[object] = []
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        if after_id:
            clauses.append("id > ?")
            params.append(after_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        if limit is not None and not after_id:
            sql = (
                f"SELECT id, payload FROM events{where} "
                f"ORDER BY id DESC LIMIT ?"
            )
            params.append(limit)
            with self.connection() as conn:
                rows = list(conn.execute(sql, params))
            for row in reversed(rows):
                try:
                    yield int(row["id"]), json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError) as e:
                    log.warning("skip corrupt event id=%s: %s", row["id"], e)
            return

        sql = f"SELECT id, payload FROM events{where} ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connection() as conn:
            for row in conn.execute(sql, params):
                try:
                    yield int(row["id"]), json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError) as e:
                    log.warning("skip corrupt event id=%s: %s", row["id"], e)

    def read_events(
        self,
        kinds: set[str] | None = None,
        *,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[dict]:
        return [
            ev for _, ev in self.iter_events(
                kinds=kinds, after_id=after_id, limit=limit
            )
        ]

    def read_events_with_ids(
        self,
        kinds: set[str] | None = None,
        *,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[tuple[int, dict]]:
        return list(
            self.iter_events(kinds=kinds, after_id=after_id, limit=limit)
        )

    # ---- migration / denorm backfill ------------------------------------

    def _meta_get(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _migrate_jsonl_once(self, conn: sqlite3.Connection) -> None:
        if self._meta_get(conn, "jsonl_migrated") == "1":
            return
        existing = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
        if existing and int(existing["c"]) > 0:
            self._meta_set(conn, "jsonl_migrated", "1")
            conn.commit()
            return

        imported = 0
        for path, source in (
            (config.EVENTS_JSONL, "live"),
            (config.BACKUP_EVENTS_JSONL, "backup"),
        ):
            n = self._import_jsonl_file(conn, path, source=source)
            if n:
                log.info("migrated %d events from %s", n, path)
                imported += n
        self._meta_set(conn, "jsonl_migrated", "1")
        conn.commit()
        if imported:
            log.info("JSONL → SQLite migration complete (%d events)", imported)

    def _import_jsonl_file(
        self, conn: sqlite3.Connection, path: Path, *, source: str
    ) -> int:
        if not path.exists():
            return 0
        n = 0
        batch: list[tuple] = []
        batch_size = 5_000
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (payload.get("kind") or "") not in HISTORY_KINDS:
                        continue
                    batch.append(self._pack(payload, source=source))
                    n += 1
                    if len(batch) >= batch_size:
                        self._flush_batch(conn, batch)
                        batch.clear()
                        log.info("…migrated %d rows from %s", n, path.name)
            if batch:
                self._flush_batch(conn, batch)
            conn.commit()
        except OSError as e:
            log.warning("could not migrate %s: %s", path, e)
        return n

    def _flush_batch(self, conn: sqlite3.Connection, batch: list[tuple]) -> None:
        conn.executemany(self._INSERT_SQL, batch)
        conn.commit()

    def _ensure_denorm(self, conn: sqlite3.Connection) -> None:
        """Backfill denorm columns, threads table, and FTS as needed."""
        if self._meta_get(conn, "denorm_v2") != "1":
            log.info("backfilling denormalized columns / FTS / threads…")
            self._backfill_event_columns(conn)
            self._rebuild_all_threads(conn)
            self._rebuild_fts(conn)
            self._meta_set(conn, "denorm_v2", "1")
            conn.commit()

        if self._meta_get(conn, "fts_standalone_v1") != "1":
            log.info("rebuilding standalone FTS index…")
            self._rebuild_fts(conn)
            self._meta_set(conn, "fts_standalone_v1", "1")
            conn.commit()

        # v3: ts_epoch ordering, guid handle merge, group titles, thread times.
        if self._meta_get(conn, "denorm_v3") != "1":
            log.info("denorm v3: epochs, handle normalize, rebuild threads…")
            self._normalize_guid_handles(conn)
            self._backfill_event_columns(conn)
            self._rebuild_all_threads(conn)
            self._meta_set(conn, "denorm_v3", "1")
            self._bump_write_seq(conn)
            conn.commit()
            log.info("denorm v3 complete")

        # v4: live used UUID-timestamp handles so backup+live duplicated every
        # message; also catch up events.jsonl the long-lived daemon still wrote.
        if self._meta_get(conn, "denorm_v4") != "1":
            log.info("denorm v4: merge UUID-timestamp handles, import jsonl…")
            self._normalize_guid_handles(conn)
            self._dedupe_by_guid(conn)
            n = self._import_jsonl_file(conn, config.EVENTS_JSONL, source="live")
            if n:
                log.info("imported %d rows from events.jsonl", n)
            self._backfill_event_columns(conn)
            self._rebuild_all_threads(conn)
            self._rebuild_fts(conn)
            self._meta_set(conn, "denorm_v4", "1")
            self._bump_write_seq(conn)
            conn.commit()
            log.info("denorm v4 complete")

        # v5: live placeholder rewrites wiped backup attachment paths; restore
        # them from message_state rows and backup_events.jsonl.
        if self._meta_get(conn, "denorm_v5") != "1":
            log.info("denorm v5: repair attachment paths on message rows…")
            n = self._repair_attachment_paths(conn)
            self._meta_set(conn, "denorm_v5", "1")
            if n:
                self._bump_write_seq(conn)
            conn.commit()
            log.info("denorm v5 complete (%d messages repaired)", n)

        # Cheap integrity: if threads empty but events exist, rebuild.
        ev = conn.execute(
            "SELECT COUNT(*) AS c FROM events "
            "WHERE kind IN ('sms_received','sms_sent')"
        ).fetchone()
        th = conn.execute("SELECT COUNT(*) AS c FROM threads").fetchone()
        if int(ev["c"] or 0) > 0 and int(th["c"] or 0) == 0:
            log.info("threads table empty — rebuilding summaries")
            self._rebuild_all_threads(conn)
            conn.commit()

    def _backfill_event_columns(self, conn: sqlite3.Connection) -> None:
        n = 0
        cursor = conn.execute("SELECT id, payload FROM events")
        while True:
            rows = cursor.fetchmany(5_000)
            if not rows:
                break
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except json.JSONDecodeError:
                    continue
                body, tkey, contact, outgoing, is_react, rtarget, epoch = (
                    _denorm_fields(payload)
                )
                conn.execute(
                    "UPDATE events SET body=?, thread_key=?, contact_name=?, "
                    "outgoing=?, is_reaction=?, reaction_target=?, ts_epoch=? "
                    "WHERE id=?",
                    (body, tkey, contact, outgoing, is_react, rtarget, epoch,
                     int(row["id"])),
                )
                n += 1
            conn.commit()
            if n and n % 50_000 == 0:
                log.info("…denorm %d rows", n)

    def _normalize_guid_handles(self, conn: sqlite3.Connection) -> None:
        """Rewrite any UUID / UUID-timestamp handle to guid:{uuid}."""
        rows = list(conn.execute(
            "SELECT id, handle, guid, payload FROM events "
            "WHERE kind IN ('sms_received', 'sms_sent')"
        ))
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {}
            if row["guid"] and not payload.get("guid"):
                payload["guid"] = row["guid"]
            if row["handle"] and not payload.get("handle"):
                payload["handle"] = row["handle"]
            new_h = stable_handle(payload)
            if new_h == row["handle"]:
                continue
            # Conflict with an existing canonical row? Drop this one after
            # copying fresher content.
            existing = conn.execute(
                "SELECT id, ts_epoch, body, payload, source, ts, thread_key "
                "FROM events WHERE handle = ?",
                (new_h,),
            ).fetchone()
            if existing is not None and int(existing["id"]) != int(row["id"]):
                cur = conn.execute(
                    "SELECT ts_epoch, body, payload, source, ts, thread_key "
                    "FROM events WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if cur and float(cur["ts_epoch"] or 0) >= float(
                    existing["ts_epoch"] or 0
                ):
                    # Prefer non-empty body and backup attachments.
                    body = cur["body"] or existing["body"]
                    try:
                        p_new = json.loads(cur["payload"] or "{}")
                        p_old = json.loads(existing["payload"] or "{}")
                    except json.JSONDecodeError:
                        p_new, p_old = {}, {}
                    if p_old.get("attachments") and not p_new.get("attachments"):
                        p_new["attachments"] = p_old["attachments"]
                    p_new["handle"] = new_h
                    conn.execute(
                        "UPDATE events SET ts=?, ts_epoch=?, body=?, payload=?, "
                        "thread_key=COALESCE(?, thread_key), source=? WHERE id=?",
                        (
                            cur["ts"] or existing["ts"],
                            cur["ts_epoch"] or existing["ts_epoch"],
                            body,
                            json.dumps(p_new, ensure_ascii=False),
                            cur["thread_key"] or existing["thread_key"],
                            cur["source"] or existing["source"],
                            existing["id"],
                        ),
                    )
                conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
            else:
                payload["handle"] = new_h
                if extract_guid(new_h) and not payload.get("guid"):
                    payload["guid"] = extract_guid(new_h)
                conn.execute(
                    "UPDATE events SET handle=?, guid=COALESCE(guid, ?), payload=? "
                    "WHERE id=?",
                    (
                        new_h,
                        extract_guid(new_h),
                        json.dumps(payload, ensure_ascii=False),
                        row["id"],
                    ),
                )
        conn.commit()

    def _dedupe_by_guid(self, conn: sqlite3.Connection) -> None:
        """One row per guid for sms_* messages (keep richest / newest)."""
        groups = list(conn.execute(
            "SELECT guid, GROUP_CONCAT(id) AS ids FROM events "
            "WHERE kind IN ('sms_received','sms_sent') "
            "AND guid IS NOT NULL AND guid != '' "
            "GROUP BY guid HAVING COUNT(*) > 1"
        ))
        for g in groups:
            ids = [int(x) for x in (g["ids"] or "").split(",") if x]
            if len(ids) < 2:
                continue
            rows = list(conn.execute(
                f"SELECT id, ts_epoch, body, payload, source, handle "
                f"FROM events WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            ))
            # Score: prefer backup (attachments), then newer ts, then longer body.
            def score(r):
                try:
                    atts = len(json.loads(r["payload"] or "{}").get("attachments") or [])
                except json.JSONDecodeError:
                    atts = 0
                return (
                    1 if r["source"] == "backup" else 0,
                    atts,
                    float(r["ts_epoch"] or 0),
                    len(r["body"] or ""),
                )
            rows.sort(key=score, reverse=True)
            keep = rows[0]
            # Ensure canonical handle.
            keep_handle = f"guid:{g['guid']}"
            if keep["handle"] != keep_handle:
                conn.execute(
                    "UPDATE events SET handle=? WHERE id=?",
                    (keep_handle, keep["id"]),
                )
            for r in rows[1:]:
                conn.execute("DELETE FROM events WHERE id=?", (r["id"],))
        conn.commit()

    def _rebuild_all_threads(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM threads")
        keys = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT thread_key FROM events "
                "WHERE thread_key IS NOT NULL AND thread_key != '' "
                "AND kind IN ('sms_received','sms_sent')"
            )
        ]
        for i, key in enumerate(keys):
            self._rebuild_thread(conn, key)
            if i and i % 2000 == 0:
                conn.commit()
                log.info("…rebuilt %d / %d threads", i, len(keys))
        conn.commit()

    def _rebuild_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("DROP TABLE IF EXISTS messages_fts")
            conn.executescript(_FTS_SCHEMA)
            conn.execute(
                "INSERT INTO messages_fts(rowid, body, contact_name) "
                "SELECT id, COALESCE(body, ''), COALESCE(contact_name, '') "
                "FROM events "
                "WHERE kind IN ('sms_received','sms_sent') AND is_reaction = 0 "
                "AND (body IS NOT NULL AND body != '')"
            )
        except sqlite3.Error as e:
            log.warning("could not rebuild FTS: %s", e)


_default_store: MessageStore | None = None
_default_lock = threading.Lock()


def default_store() -> MessageStore:
    global _default_store
    with _default_lock:
        if _default_store is None or _default_store.path != config.MESSAGES_DB:
            _default_store = MessageStore()
        return _default_store

"""Turn a device backup into events the UI already understands.

The thread store is built around the event dicts the daemon writes for MAP
messages, so rather than teaching it a second data model, backup rows are
normalized into that same shape — with extras (attachments, exact tapback
targets, reply links) that MAP could never populate.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from iphonebridge import config
from iphonebridge.backup.imessage_db import BackupMessage, read_messages
from iphonebridge.backup.runner import (
    BackupError,
    extract_attachments,
    extract_sms_db,
    list_devices,
    run_backup,
)
from iphonebridge.contacts import ContactsResolver
from iphonebridge.events import normalize_phone
from iphonebridge.imagesize import display_size
from iphonebridge.imessage.handles import group_key

log = logging.getLogger(__name__)


def _event_from(msg: BackupMessage, contacts: ContactsResolver,
                files: dict[str, Path]) -> dict:
    peer = msg.handle
    name = contacts.resolve(peer) if peer else None
    attachments = []
    for att in msg.attachments:
        local = files.get(att.get("filename") or "")
        if local is None:
            continue
        entry = {
            "path": str(local),
            "mime": att.get("mime_type") or "",
            "name": att.get("transfer_name") or local.name,
            "bytes": att.get("total_bytes") or 0,
            "is_sticker": bool(att.get("is_sticker")),
        }
        # Recorded here, once, so the UI can reserve the right space before
        # the image decodes instead of resizing the list as photos arrive.
        if entry["mime"].startswith("image/") or entry["is_sticker"]:
            size = display_size(local)
            if size:
                entry["w"], entry["h"] = size
        attachments.append(entry)
    return {
        "kind": "sms_sent" if msg.outgoing else "sms_received",
        # The GUID is globally unique and stable, so it dedupes cleanly
        # across repeated syncs — unlike MAP's per-session transfer numbers.
        "handle": f"guid:{msg.guid}",
        "guid": msg.guid,
        "sender_phone": peer,
        "sender_phone_norm": normalize_phone(peer) if peer else None,
        "contact_name": name,
        "body": msg.text or "",
        "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
        "is_read": msg.is_read,
        "raw_type": msg.service,
        "source": "backup",
        # Group chats are keyed by the chat, not by the sender — otherwise
        # every participant's messages land in their private thread and the
        # group never appears at all.
        #
        # `group_key` is the one the UI threads on, because the live iMessage
        # transport can compute the same string; `chat_guid` is a phone-local
        # sqlite id no other source can ever produce, kept only as a fallback
        # for chats whose membership didn't survive into the backup.
        "chat_guid": msg.chat_guid if msg.is_group else None,
        "group_key": (group_key(msg.participants) if msg.is_group else None),
        "chat_name": msg.chat_name,
        "is_group": msg.is_group,
        # Who actually said it, for the per-sender label in group threads.
        "sender_name": name or peer,
        "attachments": attachments,
        "reaction_verb": msg.reaction_verb,
        "reaction_target_guid": msg.reaction_target_guid,
        "reply_to_guid": msg.reply_to_guid,
    }


# Where normalized backup events are parked for the UI to pick up. Kept
# separate from the daemon's events.jsonl: that file is the daemon's to
# write, and mixing sources into it would confuse ownership.
BACKUP_EVENTS = config.STATE_DIR / "backup_events.jsonl"


def _write_events(events: list[dict]) -> None:
    """Persist normalized events so the UI can merge them on next load.

    Rewritten wholesale each sync — rows are keyed by GUID, so re-importing
    is idempotent and there's nothing to append incrementally.
    """
    try:
        config.ensure_dirs()
        tmp = BACKUP_EVENTS.with_suffix(".jsonl.tmp")
        with tmp.open("w") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        tmp.replace(BACKUP_EVENTS)          # atomic; no half-written file
        log.info("wrote %d backup events → %s", len(events), BACKUP_EVENTS)
    except OSError as e:
        log.warning("could not write backup events: %s", e)


_PRIMARY_UDID_FILE = config.STATE_DIR / "primary_device"


def _primary_udid() -> str | None:
    try:
        return _PRIMARY_UDID_FILE.read_text().strip() or None
    except OSError:
        return None


def _set_primary_udid(udid: str) -> None:
    try:
        _PRIMARY_UDID_FILE.write_text(udid + "\n")
    except OSError:
        log.warning("could not record the primary device id")


def sync(*, run_new_backup: bool = True, limit: int | None = None,
         udid: str | None = None, progress=None) -> list[dict]:
    """Back up the phone (optionally), then return normalized events.

    Raises BackupError when no device is connected or the backup can't be
    read (an encrypted backup without its password, most likely).
    """
    if run_new_backup:
        devices = list_devices()
        if not devices:
            raise BackupError(
                "no iOS device found over USB — connect the iPhone and "
                "make sure it's trusted")
        # Whichever device synced last owns the event history. Syncing a
        # different one would mix two phones' messages into one timeline,
        # so it has to be asked for explicitly.
        primary = _primary_udid()
        if primary and primary not in devices:
            raise BackupError(
                f"connected device is not the one this history came from "
                f"({primary}). Pass --udid to sync a different phone.")
        if primary and udid is None:
            udid = primary
        folder = run_backup(udid=udid, progress=progress)
        _set_primary_udid(folder.name)
    else:
        from iphonebridge.backup.runner import _backup_folder_for
        folder = _backup_folder_for(udid or _primary_udid())
        if folder is None:
            raise BackupError("no existing backup found — run a backup first")

    db = extract_sms_db(folder)
    messages = read_messages(db, limit=limit)
    log.info("backup contains %d messages", len(messages))

    wanted = [a.get("filename") for m in messages for a in m.attachments
              if a.get("filename")]
    files = extract_attachments(folder, wanted) if wanted else {}

    contacts = ContactsResolver()
    events = [_event_from(m, contacts, files) for m in messages]

    _write_events(events)

    n_att = sum(1 for e in events if e["attachments"])
    n_out = sum(1 for e in events if e["kind"] == "sms_sent")
    n_react = sum(1 for e in events if e["reaction_verb"])
    n_reply = sum(1 for e in events if e["reply_to_guid"])
    log.info("normalized: %d events (%d sent-from-phone, %d with attachments, "
             "%d tapbacks, %d replies)", len(events), n_out, n_att, n_react,
             n_reply)
    return events

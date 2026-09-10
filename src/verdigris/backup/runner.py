"""Drive `idevicebackup2` and pull files out of the resulting backup.

iOS backups don't store files under their real names — every file is saved
as `<backup>/<first two hex chars of fileID>/<fileID>`, and `Manifest.db`
is the index mapping (domain, relativePath) → fileID. So finding sms.db or
an attachment means querying the manifest first.

Requires a USB connection and a trusted pairing. Nothing here needs a Mac.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from verdigris import config

log = logging.getLogger(__name__)

BACKUP_DIR = config.STATE_DIR / "ios_backup"
ATTACHMENTS_DIR = config.STATE_DIR / "attachments"

# Where the Messages data lives inside a backup.
_SMS_DOMAIN = "HomeDomain"
_SMS_PATH = "Library/SMS/sms.db"
_ATTACHMENT_DOMAINS = ("MediaDomain", "HomeDomain")


class BackupError(RuntimeError):
    pass


@dataclass(slots=True)
class DeviceInfo:
    udid: str
    name: str | None = None
    version: str | None = None


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def list_devices() -> list[str]:
    """UDIDs of connected iOS devices."""
    proc = _run(["idevice_id", "-l"], timeout=15)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def device_info(udid: str | None = None) -> DeviceInfo | None:
    udids = [udid] if udid else list_devices()
    if not udids or not udids[0]:
        return None
    u = udids[0]

    def key(k: str) -> str | None:
        p = _run(["ideviceinfo", "-u", u, "-k", k], timeout=15)
        return p.stdout.strip() if p.returncode == 0 else None

    return DeviceInfo(udid=u, name=key("DeviceName"), version=key("ProductVersion"))


def is_paired(udid: str | None = None) -> bool:
    cmd = ["idevicepair"]
    if udid:
        cmd += ["-u", udid]
    proc = _run(cmd + ["validate"], timeout=20)
    return proc.returncode == 0


def pair(udid: str | None = None) -> tuple[bool, str]:
    """Attempt to pair. The user must tap Trust (and enter their passcode)."""
    cmd = ["idevicepair"]
    if udid:
        cmd += ["-u", udid]
    proc = _run(cmd + ["pair"], timeout=60)
    msg = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, msg


def run_backup(udid: str | None = None, *, timeout: int = 3600,
               progress=None) -> Path:
    """Run an incremental backup into BACKUP_DIR and return the device folder.

    The first backup transfers everything and can take a long time; later
    ones are incremental. `progress` receives raw output lines if given.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    cmd = ["idevicebackup2"]
    if udid:
        cmd += ["-u", udid]
    cmd += ["backup", str(BACKUP_DIR)]

    log.info("starting iOS backup into %s (this can take a while)", BACKUP_DIR)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if progress is not None:
                progress(line)
            if line:
                log.debug("backup: %s", line)
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise BackupError("backup timed out")
    if rc != 0:
        raise BackupError(f"idevicebackup2 exited {rc}")

    folder = _latest_backup_folder()
    if folder is None:
        raise BackupError("backup finished but no backup folder was found")
    log.info("backup complete: %s", folder)
    return folder


def _backup_folder_for(udid: str | None) -> Path | None:
    """The backup folder for `udid`, or the most recent one if unspecified."""
    if udid:
        folder = BACKUP_DIR / udid
        return folder if (folder / "Manifest.db").exists() else None
    return _latest_backup_folder()


def _latest_backup_folder() -> Path | None:
    """idevicebackup2 writes into BACKUP_DIR/<udid>/."""
    if not BACKUP_DIR.exists():
        return None
    candidates = [d for d in BACKUP_DIR.iterdir()
                  if d.is_dir() and (d / "Manifest.db").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: (d / "Manifest.db").stat().st_mtime)


def _backup_file_path(folder: Path, file_id: str) -> Path:
    """Backups shard files by the first two characters of the fileID."""
    return folder / file_id[:2] / file_id


def find_in_manifest(folder: Path, domain: str, relative_path: str) -> Path | None:
    manifest = folder / "Manifest.db"
    if not manifest.exists():
        return None
    con = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT fileID FROM Files WHERE domain=? AND relativePath=?",
            (domain, relative_path)).fetchone()
    except sqlite3.Error as e:
        log.warning("manifest lookup failed: %s", e)
        return None
    finally:
        con.close()
    if not row:
        return None
    path = _backup_file_path(folder, row[0])
    return path if path.exists() else None


def extract_sms_db(folder: Path, dest: Path | None = None) -> Path:
    """Copy sms.db out of the backup so it can be opened read-only."""
    src = find_in_manifest(folder, _SMS_DOMAIN, _SMS_PATH)
    if src is None:
        raise BackupError(
            "sms.db not found in the backup — if the backup is encrypted, "
            "its contents can't be read without the password")
    # Named per device. A shared "sms.db" meant plugging in a second phone
    # overwrote the first one's database with 179 messages, and the only
    # thing that saved the history was the backup folder still being there.
    dest = dest or (config.STATE_DIR / f"sms-{folder.name}.db")
    shutil.copy2(src, dest)
    # Copy the WAL/journal siblings too, or recent messages can be missing.
    for suffix in ("-wal", "-shm"):
        sib = find_in_manifest(folder, _SMS_DOMAIN, _SMS_PATH + suffix)
        if sib is not None:
            shutil.copy2(sib, dest.parent / (dest.name + suffix))
    log.info("extracted sms.db → %s (%d bytes)", dest, dest.stat().st_size)
    return dest


# ext4 caps a single filename at 255 *bytes*, and message attachments can
# carry absurdly long names (a book title, an email subject). Leave room for
# the 40-char fileID prefix and separator.
_MAX_NAME_BYTES = 200


def _safe_name(file_id: str, original: str) -> str:
    """`<fileID>_<name>` clipped to something the filesystem will accept.

    Keeps the extension, since that's what decides how the file opens.
    """
    name = Path(original).name.replace("/", "_") or "attachment"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = ext[:16]

    budget = _MAX_NAME_BYTES - len(ext) - (1 if ext else 0)
    encoded = stem.encode("utf-8", errors="ignore")[:max(1, budget)]
    # Trailing byte may be a partial multi-byte char after the cut.
    stem = encoded.decode("utf-8", errors="ignore") or "attachment"
    return f"{file_id}_{stem}{('.' + ext) if ext else ''}"


def extract_attachments(folder: Path, wanted: list[str]) -> dict[str, Path]:
    """Copy attachment files out by their sms.db `filename` values.

    sms.db stores paths like `~/Library/SMS/Attachments/ab/12/IMG_1.HEIC`;
    the manifest keys them without the `~/` prefix.
    """
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = folder / "Manifest.db"
    if not manifest.exists():
        return {}

    out: dict[str, Path] = {}
    con = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
    failed = 0
    try:
        # Index the manifest once. Doing a per-attachment `LIKE '%/name'`
        # fallback instead means a full scan of a 120k-row table for every
        # one of ~28k attachments, which is unusably slow.
        by_path: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for file_id, rel_path in con.execute(
                "SELECT fileID, relativePath FROM Files "
                "WHERE relativePath IS NOT NULL"):
            by_path[rel_path] = file_id
            by_name.setdefault(rel_path.rsplit("/", 1)[-1], file_id)

        for original in wanted:
            if not original:
                continue
            # removeprefix, not lstrip: lstrip treats the argument as a set
            # of characters and would eat any leading '~' or '/' run.
            rel = original.removeprefix("~/")
            # Exact path, else the leaf name — sms.db and the manifest don't
            # always agree byte-for-byte across iOS versions.
            file_id = by_path.get(rel) or by_name.get(Path(rel).name)
            if not file_id:
                continue

            src = _backup_file_path(folder, file_id)
            if not src.exists():
                continue
            dest = ATTACHMENTS_DIR / _safe_name(file_id, rel)
            try:
                if not dest.exists():
                    shutil.copy2(src, dest)
                out[original] = dest
            except OSError as e:
                # One unwritable file must never abort the whole import —
                # this is what killed a full run on a single over-long name.
                failed += 1
                log.debug("could not extract %s: %s", original, e)
    except sqlite3.Error as e:
        log.warning("attachment extraction failed: %s", e)
    finally:
        con.close()
    log.info("extracted %d/%d attachments (%d failed)",
             len(out), len(wanted), failed)
    return out

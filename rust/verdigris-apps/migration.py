"""Copy Blue's native configuration/cache without modifying the original data."""
from pathlib import Path
import shutil
import sqlite3
import tempfile


def copy_missing(source: Path, destination: Path) -> None:
    """Keep existing Verdigris files; snapshot SQLite through its backup API (including WAL)."""
    if not source.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_symlink() or entry.name.endswith(("-wal", "-shm", ".part")):
            continue
        if entry.is_dir():
            copy_missing(entry, target)
        elif entry.is_file() and not target.exists():
            with tempfile.TemporaryDirectory(prefix=".migration-", dir=destination) as staging:
                temporary = Path(staging) / entry.name
                if entry.suffix == ".sqlite":
                    with sqlite3.connect(entry.resolve().as_uri() + "?mode=ro", uri=True) as src:
                        with sqlite3.connect(temporary) as dst:
                            src.backup(dst)
                else:
                    shutil.copyfile(entry, temporary)
                temporary.chmod(0o600)
                # A concurrent installer must not replace a file created while copying.
                try:
                    target.hardlink_to(temporary)
                except FileExistsError:
                    pass


def migrate_native(config: Path, data: Path) -> None:
    copy_missing(config / "blue-native", config / "verdigris")
    copy_missing(data / "blue-native", data / "verdigris")

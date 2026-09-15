"""Per-iPhone-app notification preferences shared with native Settings.

Rules and the discovered-app registry are separate so Settings and the daemon
never write the same file. The registry contains app identifiers/names only.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def directory(*, data: bool = False) -> Path:
    variable, fallback = (("XDG_DATA_HOME", ".local/share") if data
                          else ("XDG_CONFIG_HOME", ".config"))
    value = os.environ.get(variable, "")
    root = Path(value) if value and Path(value).is_absolute() else Path.home() / fallback
    return root / "verdigris"


@dataclass(frozen=True)
class Rule:
    enabled: bool = True
    desktop_id: str | None = None
    icon: str | None = None

    def icon_path(self) -> str | None:
        if self.icon and Path(self.icon).name == self.icon and self.icon not in (".", ".."):
            path = directory() / "notification-icons" / self.icon
            if path.is_file():
                return str(path)
        return None


def _parse_rules(raw: bytes) -> dict[str, Rule]:
    document = json.loads(raw)
    if document.get("version") != 1 or not isinstance(document.get("apps"), dict):
        raise ValueError("Unsupported notification preferences")
    result = {}
    for app_id, values in document["apps"].items():
        if not isinstance(values, dict) or not isinstance(values.get("enabled", True), bool):
            raise ValueError("Invalid notification rule")
        for key in ("desktop_id", "icon"):
            if values.get(key) is not None and not isinstance(values[key], str):
                raise ValueError(f"Invalid {key}")
        result[app_id] = Rule(values.get("enabled", True), values.get("desktop_id"),
                              values.get("icon"))
    return result


class Preferences:
    """Reload on each event; preserve the last good rules if a file is damaged."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._raw: bytes | None = None
        self._rules: dict[str, Rule] = {}
        self._error: str | None = None

    def rule(self, app_id: str) -> Rule:
        path = directory() / "notification-rules.json"
        if self._path != path:
            self._path, self._raw, self._rules = path, None, {}
        try:
            raw = path.read_bytes()
            if raw != self._raw:
                self._rules = _parse_rules(raw)
                self._raw = raw
            self._error = None
        except FileNotFoundError:
            self._raw, self._rules = None, {}
        except (OSError, ValueError, TypeError, AttributeError) as error:
            if self._error != str(error):
                log.warning("Cannot read notification preferences; keeping last good rules: %s",
                            error)
                self._error = str(error)
        return self._rules.get(app_id, Rule())


def remember_app(app_id: str, app_name: str) -> None:
    if not app_id:
        return
    root = directory(data=True)
    path = root / "notification-apps.json"
    temporary = None
    try:
        try:
            apps = json.loads(path.read_bytes())
            if not isinstance(apps, dict) or not all(isinstance(v, str) for v in apps.values()):
                raise ValueError("Invalid notification app registry")
        except FileNotFoundError:
            apps = {}
        name = app_name or apps.get(app_id) or app_id
        if apps.get(app_id) == name:
            return
        apps[app_id] = name
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as file:
            temporary = Path(file.name)
            json.dump(apps, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    except (OSError, ValueError) as error:
        log.warning("Cannot save discovered notification app: %s", error)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


preferences = Preferences()

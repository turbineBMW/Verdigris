"""Resolve a desktop icon for an ANCS source app.

Classic ANCS gives us an app bundle identifier and display name, but not the
app's icon pixels.  Keep icon resolution local and deterministic:

1. A user-supplied icon named after the bundle identifier.
2. A matching installed Linux desktop application.
3. A recognizable generic icon for Apple apps / ANCS categories.

This intentionally does not query an Internet icon service.  Besides leaking
which apps generated notifications, remote artwork is not part of ANCS and
would make notification delivery depend on the network.
"""
from __future__ import annotations

import configparser
import os
import re
from functools import lru_cache
from pathlib import Path

from verdigris import config

_ICON_EXTENSIONS = (".png", ".svg", ".webp", ".jpg", ".jpeg")
_SAFE_BUNDLE_ID = re.compile(r"[A-Za-z0-9._-]+")

# Theme names rather than paths: the notification server resolves these in
# the active desktop icon theme.  These are fallbacks, not claims that they
# are the original iOS artwork.
_APPLE_APP_ICONS = {
    "com.apple.mobilecal": "office-calendar",
    "com.apple.mobilemail": "internet-mail",
    "com.apple.mobilesafari": "web-browser",
    "com.apple.mobilesms": "mail-message-new",
    "com.apple.reminders": "view-task",
    "com.apple.passbook": "wallet-open",
    "com.apple.mobiletimer": "appointment-soon",
    "com.apple.weather": "weather-clear",
    "com.apple.maps": "map-globe",
    "com.apple.facetime": "call-start",
    "com.apple.mobilephone": "call-start",
    "com.apple.mobileslideshow": "camera-photo",
    "com.apple.camera": "camera-photo",
    "com.apple.music": "multimedia-player",
    "com.apple.podcasts": "podcast",
    "com.apple.mobilenotes": "accessories-text-editor",
    "com.apple.news": "view-pim-news",
    "com.apple.health": "health",
    "com.apple.preferences": "preferences-system",
}

_CATEGORY_ICONS = {
    "IncomingCall": "call-start",
    "MissedCall": "call-missed",
    "Voicemail": "mail-message-new",
    "Social": "user-available",
    "Schedule": "office-calendar",
    "Email": "internet-mail",
    "News": "view-pim-news",
    "HealthAndFitness": "health",
    "BusinessAndFinance": "wallet-open",
    "Location": "map-globe",
    "Entertainment": "multimedia-player",
    "Other": "preferences-desktop-notification",
}

# Matching an installed application named "Messages" on Linux is especially
# unsafe: on this machine that is Google Messages, while ANCS means Apple's
# Messages.  Known Apple apps fall through to their generic mapping instead.
_NO_NAME_MATCH = frozenset({"com.apple.mobilesms"})


def app_icon_dir() -> Path:
    """Directory for exact, user-provided iOS app icons."""
    configured = os.environ.get("VERDIGRIS_ANCS_ICON_DIR", "").strip()
    return Path(configured).expanduser() if configured else config.STATE_DIR / "ancs_app_icons"


def _custom_icon(app_id: str) -> str | None:
    if not app_id or _SAFE_BUNDLE_ID.fullmatch(app_id) is None:
        return None
    root = app_icon_dir()
    for bundle in (app_id, app_id.lower()):
        for suffix in _ICON_EXTENSIONS:
            candidate = root / f"{bundle}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def _data_application_dirs() -> tuple[Path, ...]:
    data_home = Path(
        os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    roots = [data_home, *(Path(part) for part in data_dirs.split(":") if part)]
    # Preserve precedence while dropping duplicates.
    return tuple(dict.fromkeys(root / "applications" for root in roots))


def _desktop_entry_values(path: Path) -> tuple[str, str] | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
    except (OSError, UnicodeError, configparser.Error, KeyError):
        return None
    if entry.get("Hidden", "").casefold() == "true":
        return None
    name = entry.get("Name", "").strip()
    icon = entry.get("Icon", "").strip()
    return (name, icon) if icon else None


@lru_cache(maxsize=1)
def _desktop_icon_index() -> tuple[dict[str, str], dict[str, str]]:
    """Return (desktop-id -> icon, display-name -> icon), in XDG order."""
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for root in _data_application_dirs():
        try:
            entries = sorted(root.glob("*.desktop"))
        except OSError:
            continue
        for path in entries:
            values = _desktop_entry_values(path)
            if values is None:
                continue
            name, icon = values
            if icon.startswith("/") and not Path(icon).is_file():
                continue
            by_id.setdefault(path.stem.casefold(), icon)
            if name:
                by_name.setdefault(name.casefold(), icon)
    return by_id, by_name


def app_icon(app_id: str, app_name: str, category: str = "Other") -> str:
    """Best local icon path or theme name for an ANCS source app."""
    override = _custom_icon(app_id)
    if override:
        return override

    app_id_key = (app_id or "").casefold()
    by_id, by_name = _desktop_icon_index()
    installed = by_id.get(app_id_key)
    if installed:
        return installed
    if app_id_key not in _NO_NAME_MATCH:
        installed = by_name.get((app_name or "").strip().casefold())
        if installed:
            return installed

    mapped = _APPLE_APP_ICONS.get(app_id_key)
    if mapped:
        return mapped
    return _CATEGORY_ICONS.get(category, _CATEGORY_ICONS["Other"])

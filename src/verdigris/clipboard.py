"""Verification-code detection + system-clipboard copy.

When an incoming text carries a one-time / 2FA code, the daemon's
ClipboardSink lifts the code straight onto the clipboard so it can be
pasted without reaching for the phone.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

# A text is treated as a verification message only if it mentions one of
# these — keeps ordinary numbers (prices, dates, phone numbers) from
# triggering a copy.
_CODE_KEYWORDS = re.compile(
    r"\b(code|verification|verify|passcode|otp|one[\s-]?time|2fa|"
    r"2[\s-]?step|security|authenticat\w*|log[\s-]?in|sign[\s-]?in)\b",
    re.IGNORECASE,
)

# A code: 4-8 digits, optionally a 1-2 letter prefix (G-123456) or one
# internal separator (123-456). The lookarounds keep it from matching a
# slice of a longer run such as a 10-digit phone number.
_CODE_CANDIDATE = re.compile(
    r"(?:[A-Za-z]{1,2}-)?(?<!\d)(\d{3}[\s-]?\d{3}|\d{4,8})(?!\d)"
)


def extract_verification_code(body: str | None) -> str | None:
    """Return the verification code in `body`, or None.

    Requires both a verification keyword AND a 4-8 digit number, so an
    ordinary text that merely contains a number doesn't trigger.
    """
    if not body or not _CODE_KEYWORDS.search(body):
        return None
    candidates: list[str] = []
    for m in _CODE_CANDIDATE.finditer(body):
        digits = re.sub(r"\D", "", m.group(1))
        if 4 <= len(digits) <= 8:
            candidates.append(digits)
    if not candidates:
        return None
    # Prefer a real code over a bare 4-digit year, if both appear.
    non_year = [c for c in candidates
                if not (len(c) == 4 and 1990 <= int(c) <= 2099)]
    return (non_year or candidates)[0]


def copy_to_clipboard(text: str) -> str | None:
    """Copy `text` to the system clipboard.

    Tries wl-copy (Wayland) then xclip / xsel (X11). Returns the tool that
    worked, or None if none is installed / all failed.
    """
    # wl-copy takes the value as an argument and daemonizes to hold the
    # selection — keep its fds off pipes so subprocess.run doesn't block.
    if shutil.which("wl-copy"):
        try:
            subprocess.run(
                ["wl-copy", "--", text], check=True, timeout=5,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "wl-copy"
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("wl-copy failed: %s", e)

    for tool, cmd in (("xclip", ["xclip", "-selection", "clipboard"]),
                      ("xsel", ["xsel", "--clipboard", "--input"])):
        if shutil.which(tool) is None:
            continue
        try:
            subprocess.run(
                cmd, input=text, text=True, check=True, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return tool
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("%s failed: %s", tool, e)
    return None

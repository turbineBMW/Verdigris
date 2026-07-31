"""Converting between iphonebridge's identifiers and iMessage handles.

iMessage addresses everything as a URI-scheme'd handle — `tel:+15551234567`
or `mailto:someone@example.com` — and is strict about it: a phone number
without the `tel:` prefix, or without the `+` and country code, will not
resolve and the send fails with no useful diagnostic.

The rest of iphonebridge works in what MAP and the phonebook hand it: raw
phone strings in whatever format the sender used, plus `sender_phone_norm`,
which is digits only. So every crossing of this boundary needs a conversion,
and getting it wrong is the most likely cause of a message silently not
arriving.
"""
from __future__ import annotations

import re

# `normalize_phone` in events.py strips to digits, deliberately dropping the
# `+`. That's right for contact matching and wrong for iMessage, so we can't
# reuse its output directly — we have to put E.164 back together.
_DIGITS = re.compile(r"\D")

# North American numbers are the common case here and the one where a missing
# country code is ambiguous: 10 digits means "add +1", 11 digits starting with
# 1 means "already has it". Anything else we pass through with a `+` and let
# Apple reject it, rather than guessing at a country code we can't know.
_NANP_LENGTH = 10
_NANP_WITH_CC = 11


# Groups have no single id both transports agree on: the live protocol names a
# conversation by who is in it, while the backup's `chat.guid` is a local
# database identity that never leaves the phone. So the participant set *is*
# the identity, and this prefix marks a key derived that way.
GROUP_KEY_PREFIX = "imessage-group:"


def looks_like_email(value: str) -> bool:
    return "@" in value and not value.startswith("tel:")


def to_handle(recipient: str, *, default_country: str = "1") -> str:
    """Convert a recipient into an iMessage handle.

    Accepts what the rest of the app produces — `"+1 (555) 123-4567"`,
    `"5551234567"`, `"someone@example.com"` — as well as handles that are
    already well-formed, which pass through untouched so this is safe to
    apply more than once.
    """
    value = recipient.strip()
    if not value:
        raise ValueError("empty recipient")

    # Already a handle.
    if value.startswith(("tel:", "mailto:")):
        return value

    if looks_like_email(value):
        return f"mailto:{value}"

    digits = _DIGITS.sub("", value)
    if not digits:
        raise ValueError(f"{recipient!r} is neither a phone number nor an email")

    # A leading `+` in the original means the country code is already there,
    # whatever its length — trust it rather than applying NANP rules.
    if value.lstrip().startswith("+"):
        return f"tel:+{digits}"
    if len(digits) == _NANP_LENGTH:
        return f"tel:+{default_country}{digits}"
    if len(digits) == _NANP_WITH_CC and digits.startswith(default_country):
        return f"tel:+{digits}"
    return f"tel:+{digits}"


def from_handle(handle: str) -> str:
    """Strip the scheme, giving back something the rest of the app recognises.

    `tel:+15551234567` → `+15551234567`, `mailto:a@b.c` → `a@b.c`. The `+` is
    kept: it's meaningful for display, and `normalize_phone` discards it
    anyway when matching contacts.
    """
    for scheme in ("tel:", "mailto:"):
        if handle.startswith(scheme):
            return handle[len(scheme) :]
    return handle


def canonical_handle(value: str | None) -> str | None:
    """`to_handle` that yields None instead of raising on junk.

    Group participant lists come from two places that make no promises about
    their contents — Apple's payloads and a decade-old sqlite file — so one
    unparseable entry must not take out the whole key.
    """
    if not value:
        return None
    try:
        return to_handle(str(value))
    except ValueError:
        return None


def group_key(participants, my_handles=()) -> str | None:
    """A stable identity for a group conversation, shared by every transport.

    This is the one thing the live iMessage stream and an imported backup can
    both compute, which is the whole point: keyed on anything else they
    produce two unrelated threads for the same group, and the backup's
    thousands of messages never join up with the ones arriving now.

    Three things make it agree across sources. Handles are canonicalized,
    because the backup stores bare `+15551234567` where the live protocol says
    `tel:+15551234567`. They're deduped and sorted, so neither Apple's
    ordering nor a repeated entry can change the result. And our own handles
    are dropped — we're in every group we're in, and we're registered under
    several addresses, so including them would make the same group key
    differently depending on which of our addresses a message was sent to.

    Returns None when nothing usable survives, leaving the caller to fall back
    to whatever transport-specific id it has.
    """
    mine = {h for h in (canonical_handle(p) for p in my_handles) if h}
    others = sorted({h for h in (canonical_handle(p) for p in participants) if h} - mine)
    if not others:
        return None
    return GROUP_KEY_PREFIX + ",".join(others)


def handle_matches(handle: str, phone_norm: str | None) -> bool:
    """Whether an iMessage handle refers to the same person as a normalized phone.

    Compares on the digit tail rather than the whole string, because the
    phonebook and iMessage disagree about country codes constantly: the phone
    stores `5551234567` while iMessage says `tel:+15551234567`. Matching the
    last 10 digits catches that without treating every 1-prefixed number as
    equivalent.
    """
    if not phone_norm:
        return False
    if looks_like_email(handle):
        return False
    digits = _DIGITS.sub("", handle)
    tail = min(len(digits), len(phone_norm), _NANP_LENGTH)
    if tail == 0:
        return False
    return digits[-tail:] == phone_norm[-tail:]

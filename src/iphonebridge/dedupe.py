"""Suppressing the same message arriving over more than one transport.

iphonebridge can now learn about one message three different ways:

  * **MAP** — the iPhone notifies us over Bluetooth. Arrives for iMessages
    *and* SMS, with an obex object path as its handle.
  * **iMessage** — the native transport, with Apple's message guid.
  * **our own send** — recorded locally the moment `Send()` returns.

There is no shared identifier across these. MAP's handle is
`message389992936384029729`; iMessage's is a UUID; neither can be derived from
the other. So matching has to be heuristic: same text, compatible sender,
close together in time.

Two rules keep the heuristic from eating real messages:

1. **Only ever match across different transports.** Sending "ok" twice in a
   row is normal and must produce two rows; the same "ok" arriving via MAP and
   via iMessage is one message. Restricting matches to differing transports
   makes genuine repeats safe by construction.
2. **A short window.** Long enough to cover MAP's lag, short enough that an
   identical message later in the conversation is unaffected.

What survives is whichever copy arrives first. That's deliberate: the daemon
records the richer iMessage detail (real tapback targets, reply threading,
attachments) into its extras map *before* deduplicating, so suppressing an
iMessage row loses the row, not the metadata — which means we don't need to
delay MAP events in the hope of a better copy turning up.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# How long a message stays eligible for matching. MAP notifications can lag
# the native copy by a surprising amount when the Bluetooth link is busy;
# 90s covers what's been observed without spanning a normal conversation.
DEFAULT_WINDOW_SEC = 90.0

# Which transport an event came from. Derived from SmsEvent.raw_type, which is
# the only field that distinguishes them.
TRANSPORT_IMESSAGE = "imessage"
TRANSPORT_SENT = "sent"
TRANSPORT_MAP = "map"

_WS = re.compile(r"\s+")

# MAP and the native transport disagree about the exact quoting and spacing of
# synthesized tapback text — MAP produced 'Reacted 🎉 to  “x”' with a double
# space where the native side has one. Collapsing whitespace makes those equal.
_QUOTE_MAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def transport_of(event) -> str:
    """Classify an SmsEvent by the transport that produced it."""
    raw_type = (getattr(event, "raw_type", None) or "").lower()
    if raw_type == "imessage":
        return TRANSPORT_IMESSAGE
    if raw_type == "sms_sent":
        return TRANSPORT_SENT
    return TRANSPORT_MAP


def normalize_body(body: str | None) -> str:
    """Reduce a body to something comparable across transports."""
    if not body:
        return ""
    text = body.translate(_QUOTE_MAP)
    return _WS.sub(" ", text).strip().casefold()


def _senders_compatible(a: str | None, b: str | None) -> bool:
    """Whether two normalized senders could be the same person.

    `None` counts as compatible with anything. MAP frequently omits the sender
    entirely — every self-addressed message in testing arrived with
    `sender_phone: None` — so requiring a match would defeat the whole thing.
    Body plus a tight window is doing the real work here; the sender check
    only exists to reject an obvious mismatch.
    """
    if not a or not b:
        return True
    tail = min(len(a), len(b), 10)
    if tail == 0:
        return True
    return a[-tail:] == b[-tail:]


@dataclass
class _Seen:
    body: str
    sender: str | None
    transport: str
    at: float


class CrossTransportDeduper:
    """Remembers recent messages so a second transport's copy can be dropped.

    Not thread-safe: the daemon calls this only from the GLib main loop.
    """

    def __init__(self, window_sec: float = DEFAULT_WINDOW_SEC, limit: int = 512) -> None:
        self.window_sec = window_sec
        self.limit = limit
        self._seen: list[_Seen] = []

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_sec
        if self._seen and self._seen[0].at < cutoff:
            self._seen = [s for s in self._seen if s.at >= cutoff]
        # Hard cap, in case of a burst inside one window.
        if len(self._seen) > self.limit:
            del self._seen[: len(self._seen) - self.limit]

    def is_duplicate(self, event, *, now: float | None = None) -> bool:
        """True if this event restates one we've already emitted.

        Records the event either way, so the *next* transport's copy of it is
        recognised too.
        """
        now = time.monotonic() if now is None else now
        self._prune(now)

        body = normalize_body(getattr(event, "body", None))
        sender = getattr(event, "sender_phone_norm", None)
        transport = transport_of(event)

        # An empty body carries no evidence — a tapback-removal arrived as
        # 'Removed a laugh from “”' with nothing to match on. Never suppress
        # those; a duplicate row is better than a dropped message.
        if body:
            for prior in self._seen:
                if prior.transport == transport:
                    continue  # rule 1: only across transports
                if prior.body != body:
                    continue
                if not _senders_compatible(prior.sender, sender):
                    continue
                log.debug(
                    "suppressing %s duplicate of a %s message: %.40r",
                    transport, prior.transport, body,
                )
                return True

        self._seen.append(_Seen(body=body, sender=sender, transport=transport, at=now))
        return False

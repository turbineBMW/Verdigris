"""CallEvent — normalized phone-call event from the iPhone via HFP/oFono.

Distinct from SmsEvent and AncsEvent so sinks can render calls differently;
all three flow through the same daemon sink fan-out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from verdigris.events import normalize_phone

CallEventKind = Literal[
    "call_incoming",   # a call is ringing in
    "call_outgoing",   # we placed a call; it's dialing/ringing
    "call_active",     # the call is connected (or held)
    "call_ended",      # the call object went away
]

# oFono VoiceCall.State → our coarse event kind.
_STATE_KIND: dict[str, CallEventKind] = {
    "incoming": "call_incoming",
    "waiting": "call_incoming",
    "dialing": "call_outgoing",
    "alerting": "call_outgoing",
    "active": "call_active",
    "held": "call_active",
}


def kind_for_state(state: str, *, ended: bool = False) -> CallEventKind:
    """Map an oFono VoiceCall.State string to a CallEventKind."""
    if ended:
        return "call_ended"
    return _STATE_KIND.get(state, "call_active")


@dataclass(slots=True)
class CallEvent:
    """A single phone-call lifecycle event mirrored from the iPhone."""

    kind: CallEventKind
    call_path: str                         # oFono VoiceCall object path
    direction: Literal["incoming", "outgoing"]
    state: str                             # raw oFono VoiceCall.State
    peer_phone: str | None                 # remote number (raw LineIdentification)
    peer_phone_norm: str | None            # digits-only, for contact lookup
    contact_name: str | None               # resolved from the contacts cache
    peer_name: str | None                  # name oFono got from the network
    seen_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def display_peer(self) -> str:
        """Best human-readable label for the other party."""
        return (self.contact_name or self.peer_name
                or self.peer_phone or "(unknown)")

    def to_dict(self) -> dict:
        """Serializable form for the JSONL log and D-Bus signals."""
        return {
            "kind": self.kind,
            "call_path": self.call_path,
            "direction": self.direction,
            "state": self.state,
            "peer_phone": self.peer_phone,
            "peer_phone_norm": self.peer_phone_norm,
            "contact_name": self.contact_name,
            "peer_name": self.peer_name,
            "seen_at": self.seen_at.isoformat(),
        }


def call_event_from_ofono(
    call_path: str,
    props: dict,
    *,
    direction: str,
    contact_name: str | None = None,
    ended: bool = False,
) -> CallEvent:
    """Build a CallEvent from an oFono org.ofono.VoiceCall properties dict.

    `props` needs `State`, optionally `LineIdentification` and `Name`.
    When `ended` is set the kind is forced to call_ended and state to
    'disconnected' (CallRemoved carries no fresh properties).
    """
    state = str(props.get("State", "") or "")
    peer_raw = props.get("LineIdentification")
    peer_raw = str(peer_raw) if peer_raw not in (None, "") else None
    peer_name = props.get("Name")
    peer_name = str(peer_name) if peer_name not in (None, "") else None
    return CallEvent(
        kind=kind_for_state(state, ended=ended),
        call_path=call_path,
        direction="incoming" if direction == "incoming" else "outgoing",
        state="disconnected" if ended else state,
        peer_phone=peer_raw,
        peer_phone_norm=normalize_phone(peer_raw),
        contact_name=contact_name,
        peer_name=peer_name,
    )

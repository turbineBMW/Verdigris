"""JSONL event log sink.

Appends one JSON object per line to ~/.local/state/iphonebridge/events.jsonl.
Useful for debugging, replay-tuning future correlator logic, and as the
durable record before SQLite catches up.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from iphonebridge import config
from iphonebridge.events import SmsEvent

log = logging.getLogger(__name__)


class JsonlSink:
    name = "jsonl"

    def __init__(self, path: Path | None = None) -> None:
        config.ensure_dirs()
        self.path = path or config.EVENTS_JSONL
        log.info("jsonl sink → %s", self.path)

    def handle(self, event: SmsEvent) -> None:
        self._append(event.to_dict())

    def handle_ancs(self, event) -> None:
        """Same JSONL, kind: 'ancs_notification' instead of 'sms_received'."""
        self._append(event.to_dict())

    def handle_call(self, event) -> None:
        """Same JSONL, kind: 'call_*' (see CallEvent.to_dict)."""
        self._append(event.to_dict())

    def handle_state(self, props: dict) -> None:
        """Delivery/read/edit/unsend — updates an existing message, not a new one.

        Written so a UI restart can rebuild captions and edited text. Typing
        is deliberately not persisted (ephemeral).
        """
        state = props.get("state") or ""
        if state in ("typing", "typing_stopped"):
            return
        payload = {
            "kind": "message_state",
            # Stable-ish id so reloads can skip a line already applied.
            "handle": (
                props.get("handle_id")
                or "state:{guid}:{state}:{ts}".format(
                    guid=props.get("guid") or "",
                    state=state,
                    ts=props.get("timestamp") or "",
                )
            ),
            "guid": props.get("guid") or "",
            "state": state,
            "peer_handle": props.get("handle") or "",
            "timestamp": props.get("timestamp") or "",
            "body": props.get("body") or "",
        }
        self._append(payload)

    def _append(self, payload: dict) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("jsonl write failed: %s", e)

"""SQLite sink — durable message history for the daemon.

Writes every SMS/iMessage event and message_state record into
`~/.local/state/iphonebridge/messages.sqlite`. The Qt UI and CLI read from
the same file (see `iphonebridge.message_store`).
"""
from __future__ import annotations

import logging
from pathlib import Path

from iphonebridge.events import SmsEvent
from iphonebridge.message_store import MessageStore

log = logging.getLogger(__name__)


class SqliteSink:
    name = "sqlite"

    def __init__(self, path: Path | None = None) -> None:
        self._store = MessageStore(path)
        # Open eagerly so migration runs at daemon startup, not mid-message.
        self._store.open()
        log.info("sqlite sink → %s", self._store.path)

    @property
    def store(self) -> MessageStore:
        return self._store

    def handle(self, event: SmsEvent) -> None:
        self._store.upsert_event(event.to_dict(), source="live")

    def handle_state(self, props: dict) -> None:
        self._store.upsert_state(props)

    # ANCS and call events are not conversation history — skip them so the
    # messages DB stays a message store. (The old JSONL mixed them in.)
    def handle_ancs(self, event) -> None:  # noqa: ARG002
        return

    def handle_call(self, event) -> None:  # noqa: ARG002
        return

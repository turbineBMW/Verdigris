"""Sinks — consumers of normalized verdigris events.

Each sink implements `handle(event)` for SMS/iMessage events. The daemon
calls every registered sink for every event. Failures in one sink should
not affect the others.

A sink may *optionally* also implement:
  • handle_ancs(event: AncsEvent) — per-app notifications (ANCS)
  • handle_call(event: CallEvent) — phone-call lifecycle (HFP)
The daemon duck-types these via getattr, so a sink opts in simply by
defining the method.
"""
from __future__ import annotations

from typing import Protocol

from verdigris.events import SmsEvent


class Sink(Protocol):
    name: str

    def handle(self, event: SmsEvent) -> None: ...

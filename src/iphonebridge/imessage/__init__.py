"""Native iMessage, via Apple directly rather than via the phone.

Every other transport in iphonebridge goes through the iPhone over Bluetooth,
and MAP sets a hard ceiling on what that can do: incoming text only, nothing
you sent, no attachments, no reliable tapback targets, no reply threading, and
sending limited to plain text. This package lifts that ceiling by talking to
Apple's IDS/APNs directly as the user's own Apple ID.

The protocol work happens in a separate Rust process (`rust/ib-imessage`),
which links rustpush and speaks line-delimited JSON over a unix socket. See
`client.IMessageClient` for the Python end.
"""
from __future__ import annotations

from .client import IMessageClient, IMessageError, IMessageUnavailable

__all__ = ["IMessageClient", "IMessageError", "IMessageUnavailable"]

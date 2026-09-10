"""Minimal bMessage parser.

The Bluetooth MAP spec wraps each SMS in a 'bMessage' container:

    BEGIN:BMSG
    VERSION:1.0
    STATUS:UNREAD
    TYPE:SMS_GSM
    FOLDER:telecom/msg/inbox
    BEGIN:VCARD                ← originator
    VERSION:2.1
    N:Smith;John;;;
    TEL:+15551234567
    END:VCARD
    BEGIN:BENV
    BEGIN:BBODY
    CHARSET:UTF-8
    LENGTH:21
    BEGIN:MSG
    Hello from my phone
    END:MSG
    END:BBODY
    END:BENV
    END:BMSG

We extract sender (TEL from the first VCARD) + body (between BEGIN:MSG /
END:MSG) and ignore everything else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_VCARD_RE = re.compile(
    r"BEGIN:VCARD(?P<body>.*?)END:VCARD", re.DOTALL | re.IGNORECASE
)
_MSG_BODY_RE = re.compile(
    r"BEGIN:MSG\s*\r?\n(?P<body>.*?)\s*END:MSG", re.DOTALL | re.IGNORECASE
)
_BMSG_STATUS_RE = re.compile(r"^STATUS:(?P<v>\S+)", re.MULTILINE | re.IGNORECASE)
_BMSG_TYPE_RE   = re.compile(r"^TYPE:(?P<v>\S+)",   re.MULTILINE | re.IGNORECASE)
_BMSG_FOLDER_RE = re.compile(r"^FOLDER:(?P<v>\S+)", re.MULTILINE | re.IGNORECASE)


@dataclass(slots=True)
class ParsedBMessage:
    sender_phone: str | None
    sender_name: str | None
    body: str | None
    status: str | None       # "READ" / "UNREAD"
    type: str | None         # "SMS_GSM" etc.
    folder: str | None


def parse(blob: str) -> ParsedBMessage:
    # First VCARD = originator (for incoming SMS)
    sender_phone: str | None = None
    sender_name: str | None = None
    m = _VCARD_RE.search(blob)
    if m:
        vc = m.group("body")
        for line in vc.splitlines():
            line = line.strip()
            if not line:
                continue
            up = line.upper()
            if up.startswith("FN:"):
                sender_name = line[3:].strip() or None
            elif up.startswith("N:") and sender_name is None:
                # N: surname; first; middle; prefix; suffix
                parts = line[2:].split(";")
                joined = " ".join(p.strip() for p in (parts[1:2] + parts[0:1])
                                  if p.strip())
                sender_name = joined or None
            elif up.startswith("TEL"):
                _, _, val = line.partition(":")
                if val.strip() and sender_phone is None:
                    sender_phone = val.strip()

    body_m = _MSG_BODY_RE.search(blob)
    body = body_m.group("body").strip("\r\n ") if body_m else None
    # Strip MAP byte-stuffing (the leading space convention for lines that
    # would otherwise start with `END:`).
    if body:
        body = "\n".join(
            line[1:] if line.startswith(" ") else line
            for line in body.splitlines()
        )

    def _first(rx) -> str | None:
        mm = rx.search(blob)
        return mm.group("v") if mm else None

    return ParsedBMessage(
        sender_phone=sender_phone,
        sender_name=sender_name,
        body=body,
        status=_first(_BMSG_STATUS_RE),
        type=_first(_BMSG_TYPE_RE),
        folder=_first(_BMSG_FOLDER_RE),
    )

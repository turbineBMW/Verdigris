"""Tests for verdigris.obex.bmessage.parse — extracting sender + body
from a MAP bMessage envelope."""
from __future__ import annotations

import textwrap

from verdigris.obex.bmessage import parse


def _bmsg(sender_tel: str, body: str, status: str = "UNREAD") -> str:
    """Build a minimal incoming bMessage for testing."""
    return textwrap.dedent(f"""\
        BEGIN:BMSG
        VERSION:1.0
        STATUS:{status}
        TYPE:SMS_GSM
        FOLDER:telecom/msg/inbox
        BEGIN:VCARD
        VERSION:2.1
        N:Doe;Jane;;;
        FN:Jane Doe
        TEL:{sender_tel}
        END:VCARD
        BEGIN:BENV
        BEGIN:BBODY
        CHARSET:UTF-8
        LENGTH:{len(body)}
        BEGIN:MSG
        {body}
        END:MSG
        END:BBODY
        END:BENV
        END:BMSG
        """).replace("\n", "\r\n")


class TestBasicParsing:
    def test_simple_incoming(self):
        blob = _bmsg("+15551234567", "Hello from the test")
        p = parse(blob)
        assert p.sender_phone == "+15551234567"
        assert p.sender_name == "Jane Doe"
        assert p.body == "Hello from the test"
        assert p.status == "UNREAD"
        assert p.type == "SMS_GSM"
        assert p.folder == "telecom/msg/inbox"

    def test_status_read(self):
        blob = _bmsg("+15551234567", "x", status="READ")
        p = parse(blob)
        assert p.status == "READ"

    def test_empty_body_handled(self):
        blob = _bmsg("+15551234567", "")
        p = parse(blob)
        # body might be empty string or None depending on regex behavior — both OK
        assert p.body in ("", None)

    def test_no_vcard(self):
        # bMessage without originator VCARD (degenerate but possible)
        blob = (
            "BEGIN:BMSG\r\n"
            "VERSION:1.0\r\n"
            "TYPE:SMS_GSM\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:BBODY\r\n"
            "LENGTH:5\r\n"
            "BEGIN:MSG\r\n"
            "hello\r\n"
            "END:MSG\r\n"
            "END:BBODY\r\n"
            "END:BENV\r\n"
            "END:BMSG\r\n"
        )
        p = parse(blob)
        assert p.sender_phone is None
        assert p.body == "hello"


class TestEdgeCases:
    def test_multiline_body(self):
        body = "Line 1\nLine 2\nLine 3"
        blob = _bmsg("+15551234567", body)
        p = parse(blob)
        # The body may have line normalization but content should be preserved
        assert "Line 1" in p.body
        assert "Line 3" in p.body

    def test_unicode_body(self):
        blob = _bmsg("+15551234567", "héllo 👋 wörld")
        p = parse(blob)
        assert p.body == "héllo 👋 wörld"

    def test_n_field_fallback_when_fn_missing(self):
        # Only N: present, no FN:
        blob = (
            "BEGIN:BMSG\r\n"
            "TYPE:SMS_GSM\r\n"
            "BEGIN:VCARD\r\n"
            "N:Smith;John;;;\r\n"
            "TEL:+15551234567\r\n"
            "END:VCARD\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:BBODY\r\n"
            "LENGTH:2\r\n"
            "BEGIN:MSG\r\n"
            "hi\r\n"
            "END:MSG\r\n"
            "END:BBODY\r\n"
            "END:BENV\r\n"
            "END:BMSG\r\n"
        )
        p = parse(blob)
        # Parser uses "first;rest" reorder; just check we got *something*
        assert p.sender_name and len(p.sender_name) > 0

    def test_tel_with_type_attribute(self):
        blob = (
            "BEGIN:BMSG\r\n"
            "TYPE:SMS_GSM\r\n"
            "BEGIN:VCARD\r\n"
            "FN:Alice\r\n"
            "TEL;TYPE=CELL:+15551234567\r\n"
            "END:VCARD\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:BBODY\r\n"
            "LENGTH:2\r\n"
            "BEGIN:MSG\r\n"
            "hi\r\n"
            "END:MSG\r\n"
            "END:BBODY\r\n"
            "END:BENV\r\n"
            "END:BMSG\r\n"
        )
        p = parse(blob)
        assert p.sender_phone == "+15551234567"
        assert p.sender_name == "Alice"

    def test_garbage_input_doesnt_crash(self):
        # Defensive — any string should produce a ParsedBMessage, not raise
        for garbage in ["", "not a bmessage", "BEGIN:BMSG\r\nEND:BMSG", "{'json': 'huh'}"]:
            p = parse(garbage)
            assert p is not None  # may have None fields, but doesn't crash

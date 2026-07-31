"""Tests for iphonebridge.events — phone normalization, timestamp parsing,
SmsEvent construction from MAP Message1 properties."""
from __future__ import annotations

from datetime import datetime

import pytest

from iphonebridge.events import (
    SmsEvent,
    normalize_phone,
    parse_map_timestamp,
    sms_event_from_message1_props,
    sms_sent_event,
)


class TestNormalizePhone:
    @pytest.mark.parametrize("raw,expected", [
        ("+1 (555) 123-4567", "15551234567"),
        ("+15551234567",      "15551234567"),
        ("5551234567",        "5551234567"),
        ("(555) 123-4567",    "5551234567"),
        ("555.123.4567",      "5551234567"),
        ("555 123 4567",      "5551234567"),
        ("+1-555-123-4567",   "15551234567"),
    ])
    def test_real_numbers_pass(self, raw, expected):
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "Mom", "123", "abcdef"])
    def test_non_phones_return_none(self, raw):
        assert normalize_phone(raw) is None

    def test_short_digit_string_returns_none(self):
        # 6 digits is below threshold
        assert normalize_phone("555-1234") == "5551234"  # 7 digits is the boundary
        assert normalize_phone("12345") is None          # 5 digits is too short


class TestParseMapTimestamp:
    def test_basic_format(self):
        result = parse_map_timestamp("20260519T181423")
        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 19
        assert result.hour == 18
        assert result.minute == 14
        assert result.second == 23

    def test_with_tz_suffix(self):
        # iPhone may append a TZ offset; we just take the first 15 chars
        result = parse_map_timestamp("20260519T181423+0500")
        assert result is not None
        assert result.day == 19

    @pytest.mark.parametrize("bad", ["", None, "not a date", "20260", "abcdef"])
    def test_invalid_returns_none(self, bad):
        assert parse_map_timestamp(bad) is None


class TestSmsEventFromProps:
    def test_full_payload(self):
        props = {
            "Sender": "+15551234567",
            "Subject": "hello world",
            "Timestamp": "20260519T120000",
            "Type": "SMS_GSM",
            "Status": "complete",
            "Read": False,
        }
        e = sms_event_from_message1_props("message123", props,
                                          contact_name="Test Contact")
        assert e.handle == "message123"
        assert e.sender_phone == "+15551234567"
        assert e.sender_phone_norm == "15551234567"
        assert e.contact_name == "Test Contact"
        assert e.body == "hello world"
        assert e.is_read is False
        assert e.raw_type == "SMS_GSM"
        assert e.timestamp is not None
        assert e.timestamp.day == 19

    def test_no_contact_resolves_to_phone(self):
        props = {"Sender": "+15551234567", "Subject": "hi"}
        e = sms_event_from_message1_props("h1", props, contact_name=None)
        assert e.display_sender == "+15551234567"

    def test_no_sender_at_all(self):
        e = sms_event_from_message1_props("h2", {}, contact_name=None)
        assert e.display_sender == "(unknown)"
        assert e.sender_phone is None
        assert e.sender_phone_norm is None

    def test_to_dict_is_json_serializable(self):
        import json
        props = {"Sender": "+15551234567", "Subject": "hi",
                 "Timestamp": "20260519T120000"}
        e = sms_event_from_message1_props("h3", props,
                                          contact_name="Alice")
        d = e.to_dict()
        # Round-trips through JSON cleanly
        s = json.dumps(d)
        parsed = json.loads(s)
        assert parsed["body"] == "hi"
        assert parsed["contact_name"] == "Alice"
        assert parsed["sender_phone_norm"] == "15551234567"

    def test_sender_address_used_if_sender_missing(self):
        props = {"SenderAddress": "+15551234567", "Subject": "hi"}
        e = sms_event_from_message1_props("h4", props)
        assert e.sender_phone == "+15551234567"


class TestSmsSentEvent:
    def test_recipient_lands_in_sender_fields(self):
        # A sent event carries the recipient in sender_* so it threads with
        # incoming messages from the same person.
        e = sms_sent_event("+15551234567", "on my way",
                           contact_name="Robin",
                           transfer_path="/org/bluez/obex/client/session0/transfer3")
        assert e.kind == "sms_sent"
        assert e.sender_phone == "+15551234567"
        assert e.sender_phone_norm == "15551234567"
        assert e.contact_name == "Robin"
        assert e.body == "on my way"
        assert e.is_read is True
        assert e.handle.startswith("transfer3-")
        assert e.timestamp is not None
        assert e.display_sender == "Robin"

    def test_handles_are_unique_across_sessions(self):
        """obexd restarts its transfer counter at zero, so the bare path tail
        collides across sessions and a colliding handle drops a real message."""
        path = "/org/bluez/obex/client/session0/transfer3"
        first = sms_sent_event("+15551234567", "one", transfer_path=path)
        second = sms_sent_event("+15551234567", "two", transfer_path=path)
        assert first.handle != second.handle

    def test_handle_synthesized_without_transfer_path(self):
        e = sms_sent_event("+15551234567", "hi")
        assert e.handle.startswith("sent-")

    def test_to_dict_is_json_serializable(self):
        import json
        e = sms_sent_event("+15551234567", "hi", contact_name="Alice")
        parsed = json.loads(json.dumps(e.to_dict()))
        assert parsed["kind"] == "sms_sent"
        assert parsed["contact_name"] == "Alice"
        assert parsed["body"] == "hi"


class TestSmsEventDisplay:
    def test_prefers_contact_over_phone(self):
        e = SmsEvent(
            kind="sms_received", handle="h", sender_phone="+15551234567",
            sender_phone_norm="15551234567", contact_name="Alice",
            body="hi", timestamp=None, is_read=False,
            raw_status=None, raw_type=None,
        )
        assert e.display_sender == "Alice"

    def test_falls_back_to_phone(self):
        e = SmsEvent(
            kind="sms_received", handle="h", sender_phone="+15551234567",
            sender_phone_norm="15551234567", contact_name=None,
            body="hi", timestamp=None, is_read=False,
            raw_status=None, raw_type=None,
        )
        assert e.display_sender == "+15551234567"

    def test_unknown_fallback(self):
        e = SmsEvent(
            kind="sms_received", handle="h", sender_phone=None,
            sender_phone_norm=None, contact_name=None,
            body=None, timestamp=None, is_read=False,
            raw_status=None, raw_type=None,
        )
        assert e.display_sender == "(unknown)"

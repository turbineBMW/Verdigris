"""Tests for verdigris.hfp.events — CallEvent construction from oFono
VoiceCall properties, state→kind mapping, caller-ID handling."""
from __future__ import annotations

import json

import pytest

from verdigris.hfp.events import (
    CallEvent,
    call_event_from_ofono,
    kind_for_state,
)


class TestKindForState:
    @pytest.mark.parametrize("state,kind", [
        ("incoming", "call_incoming"),
        ("waiting",  "call_incoming"),
        ("dialing",  "call_outgoing"),
        ("alerting", "call_outgoing"),
        ("active",   "call_active"),
        ("held",     "call_active"),
    ])
    def test_state_maps_to_kind(self, state, kind):
        assert kind_for_state(state) == kind

    def test_unknown_state_defaults_to_active(self):
        assert kind_for_state("nonsense") == "call_active"

    def test_ended_overrides_everything(self):
        assert kind_for_state("active", ended=True) == "call_ended"
        assert kind_for_state("incoming", ended=True) == "call_ended"


class TestCallEventFromOfono:
    def test_incoming_with_caller_id(self):
        e = call_event_from_ofono(
            "/hfp/org/bluez/hci0/dev_14_1B_A0_D6_E6_1D/voicecall01",
            {"State": "incoming", "LineIdentification": "+14075550105",
             "Name": ""},
            direction="incoming",
        )
        assert e.kind == "call_incoming"
        assert e.direction == "incoming"
        assert e.state == "incoming"
        assert e.peer_phone == "+14075550105"
        assert e.peer_phone_norm == "14075550105"
        assert e.display_peer == "+14075550105"

    def test_contact_name_wins_for_display(self):
        e = call_event_from_ofono(
            "/c", {"State": "incoming", "LineIdentification": "+14075550105"},
            direction="incoming", contact_name="Alex",
        )
        assert e.contact_name == "Alex"
        assert e.display_peer == "Alex"

    def test_withheld_caller_id(self):
        e = call_event_from_ofono(
            "/c", {"State": "incoming", "LineIdentification": ""},
            direction="incoming",
        )
        assert e.peer_phone is None
        assert e.peer_phone_norm is None
        assert e.display_peer == "(unknown)"

    def test_outgoing_dialing(self):
        e = call_event_from_ofono(
            "/c", {"State": "dialing", "LineIdentification": "+15551234567"},
            direction="outgoing",
        )
        assert e.kind == "call_outgoing"
        assert e.direction == "outgoing"

    def test_ended_forces_kind_and_state(self):
        e = call_event_from_ofono(
            "/c", {"State": "active", "LineIdentification": "+15551234567"},
            direction="outgoing", ended=True,
        )
        assert e.kind == "call_ended"
        assert e.state == "disconnected"

    def test_network_name_used_when_no_contact(self):
        e = call_event_from_ofono(
            "/c", {"State": "active", "LineIdentification": "",
                   "Name": "ACME Corp"},
            direction="incoming",
        )
        assert e.peer_name == "ACME Corp"
        assert e.display_peer == "ACME Corp"

    def test_to_dict_is_json_serializable(self):
        e = call_event_from_ofono(
            "/hfp/.../voicecall01",
            {"State": "active", "LineIdentification": "+14075550105",
             "Name": ""},
            direction="outgoing", contact_name="Alex",
        )
        d = e.to_dict()
        parsed = json.loads(json.dumps(d))
        assert parsed["kind"] == "call_active"
        assert parsed["direction"] == "outgoing"
        assert parsed["contact_name"] == "Alex"
        assert parsed["call_path"] == "/hfp/.../voicecall01"
        assert parsed["peer_phone_norm"] == "14075550105"


class TestCallEventDisplay:
    def test_display_peer_fallback_order(self):
        # contact_name > peer_name > peer_phone > "(unknown)"
        base = dict(kind="call_active", call_path="/c", direction="incoming",
                    state="active", peer_phone="+15551234567",
                    peer_phone_norm="15551234567")
        assert CallEvent(**base, contact_name="C", peer_name="N").display_peer == "C"
        assert CallEvent(**base, contact_name=None, peer_name="N").display_peer == "N"
        assert CallEvent(**base, contact_name=None,
                         peer_name=None).display_peer == "+15551234567"
        bare = dict(base, peer_phone=None, peer_phone_norm=None)
        assert CallEvent(**bare, contact_name=None,
                         peer_name=None).display_peer == "(unknown)"

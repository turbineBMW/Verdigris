"""Tests for verdigris.clipboard — verification-code detection."""
from __future__ import annotations

import pytest

from verdigris.clipboard import extract_verification_code


class TestExtractVerificationCode:
    @pytest.mark.parametrize("body,expected", [
        ("474229 is your Instagram code. Don't share it.", "474229"),
        ("Your verification code is 123456", "123456"),
        ("G-558211 is your Google verification code.", "558211"),
        ("Your Apple ID Code is: 901234. Do not share it.", "901234"),
        ("Use 1234 to verify your number.", "1234"),
        ("Your one-time passcode is 12345678.", "12345678"),
        ("Your code is 123-456.", "123456"),
        ("PayPal: 778 990 is your security code.", "778990"),
        ("Enter 55213 to sign in.", "55213"),
    ])
    def test_detects_real_codes(self, body, expected):
        assert extract_verification_code(body) == expected

    @pytest.mark.parametrize("body", [
        "Hey, are you free at 7?",
        "Call me back at 5551234567",          # 10 digits, and no keyword
        "See you in 2026!",
        "Your package 4471123 has shipped",    # digits but no keyword
        "Running late, be there in 15",
        "",
        None,
    ])
    def test_ignores_non_codes(self, body):
        assert extract_verification_code(body) is None

    def test_keyword_alone_without_a_number(self):
        assert extract_verification_code("Check the code on the door") is None

    def test_year_not_picked_when_a_real_code_is_present(self):
        assert extract_verification_code(
            "Your login code 558211 expires in 2026") == "558211"

    def test_long_phone_number_not_treated_as_code(self):
        # Has the keyword 'code' but the only number is an 11-digit phone.
        assert extract_verification_code(
            "Text the code to 15551234567") is None

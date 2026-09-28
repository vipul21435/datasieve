"""Detector behaviour, including the false-positive guards each one promises."""

from __future__ import annotations

import random

import pytest

from curator.pii import KINDS
from curator.pii import Detection
from curator.pii import detect
from curator.pii import detect_api_keys
from curator.pii import detect_credit_cards
from curator.pii import detect_emails
from curator.pii import detect_ipv4
from curator.pii import detect_ipv6
from curator.pii import detect_phones
from curator.pii import luhn_valid
from curator.pii import scrub
from curator.pii import shannon_entropy


def _kinds(text: str) -> list[str]:
    return [d.kind for d in detect(text)]


def _luhn_complete(prefix: str) -> str:
    """Append the check digit that makes ``prefix`` Luhn-valid."""
    for check in "0123456789":
        if luhn_valid(prefix + check):
            return prefix + check
    pytest.fail("no check digit found")


@pytest.mark.parametrize(
    "text",
    ["reach me at jane.doe+work@example.co.uk today", "<bob@corp.io>", "mail: a_b@x-y.org."],
)
def test_email_detected(text: str) -> None:
    found = detect_emails(text)
    assert len(found) == 1
    assert found[0].kind == "email"
    assert "@" in found[0].text


@pytest.mark.parametrize("text", ["user@localhost", "@handle", "price @ 5 dollars", "a@b"])
def test_email_false_positives(text: str) -> None:
    assert detect_emails(text) == []


@pytest.mark.parametrize(
    "text",
    ["call +1 415-555-0100", "tel (020) 7946 0958", "+44 20 7946 0958", "555.867.5309 x", "+919876543210"],
)
def test_phone_detected(text: str) -> None:
    assert [d.kind for d in detect_phones(text)] == ["phone"]


@pytest.mark.parametrize(
    "text",
    ["order 1234567890", "version 1.2.3", "year 2024 and 1999", "at 12:30:45", "1111111111", "pi is 3.14159265"],
)
def test_phone_false_positives(text: str) -> None:
    assert detect_phones(text) == []


@pytest.mark.parametrize("text", ["host 10.0.0.1 up", "192.168.1.254.", "255.255.255.255"])
def test_ipv4_detected(text: str) -> None:
    assert [d.kind for d in detect_ipv4(text)] == ["ipv4"]


@pytest.mark.parametrize("text", ["version 1.2.3.4.5", "256.1.1.1", "1.2.3", "build 10.0.0.1a"])
def test_ipv4_false_positives(text: str) -> None:
    assert detect_ipv4(text) == []


@pytest.mark.parametrize("text", ["addr fe80::1 up", "2001:db8:85a3::8a2e:370:7334", "loopback ::1 ok"])
def test_ipv6_detected(text: str) -> None:
    assert [d.kind for d in detect_ipv6(text)] == ["ipv6"]


@pytest.mark.parametrize("text", ["std::vector<int>", "time 12:30:45", "mac aa:bb:cc:dd:ee:ff", "a::b", "x::y::z"])
def test_ipv6_false_positives(text: str) -> None:
    assert detect_ipv6(text) == []


@pytest.mark.parametrize("text", ["card 4111 1111 1111 1111", "4111-1111-1111-1111", "amex 378282246310005 ok"])
def test_credit_card_detected(text: str) -> None:
    assert [d.kind for d in detect_credit_cards(text)] == ["credit_card"]


@pytest.mark.parametrize(
    "text",
    ["4111 1111 1111 1112", "0000000000000000", "id 1234567890123", "ref 123456789012345678901"],
)
def test_credit_card_false_positives(text: str) -> None:
    assert detect_credit_cards(text) == []


@pytest.mark.parametrize(
    "text",
    ["card 4111 1111 1111 1112", "serial 1234 5678 9012 3456", "ref 1234-5678-9012-3456-7890"],
)
def test_luhn_failing_runs_are_not_scrubbed_as_phones(text: str) -> None:
    result = scrub(text)
    assert result.text == text
    assert result.counts == {}
    assert detect(text) == []


def test_credit_card_seeded_luhn_numbers() -> None:
    rng = random.Random(20260929)  # noqa: S311
    for _ in range(50):
        length = rng.choice([13, 15, 16, 19])
        prefix = "4" + "".join(rng.choice("0123456789") for _ in range(length - 2))
        number = _luhn_complete(prefix)
        assert luhn_valid(number)
        assert [d.kind for d in detect_credit_cards(f"pay with {number} now")] == ["credit_card"]
        broken = number[:-1] + str((int(number[-1]) + 1) % 10)
        assert detect_credit_cards(f"pay with {broken} now") == []


@pytest.mark.parametrize(
    "text",
    [
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
        "sk-ant-api03-abcdefghijklmnop1234",
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcd12",
        "github_pat_11ABCDEFG0123456789abcdefgh",
        "xoxb-1234567890-abcdefghijk",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_api_key_prefixes(text: str) -> None:
    assert [d.kind for d in detect_api_keys(text)] == ["api_key"]


def test_api_key_high_entropy_seeded() -> None:
    rng = random.Random(7)  # noqa: S311
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    for _ in range(25):
        token = "".join(rng.choice(alphabet) for _ in range(40))
        assert shannon_entropy(token) >= 4.0
        assert [d.kind for d in detect_api_keys(f"secret={token}")] == ["api_key"]


@pytest.mark.parametrize(
    "text",
    [
        "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "md5 d41d8cd98f00b204e9800998ecf8427e",
        "uuid 123e4567-e89b-12d3-a456-426614174000",
        "this_is_a_very_long_identifier_name_for_a_function_1",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
        "0123456789012345678901234567890123456789",
    ],
)
def test_api_key_false_positives(text: str) -> None:
    assert detect_api_keys(text) == []


def test_detect_resolves_overlaps_by_priority() -> None:
    found = detect("card 4111 1111 1111 1111 and phone +1 415-555-0100")
    assert [d.kind for d in found] == ["credit_card", "phone"]
    assert found[0].text == "4111 1111 1111 1111"


def test_detect_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown detector"):
        detect("x", kinds=["ssn"])


def test_detect_subset_of_kinds() -> None:
    assert _kinds("a@b.io 10.0.0.1") == ["email", "ipv4"]
    assert [d.kind for d in detect("a@b.io 10.0.0.1", kinds=["ipv4"])] == ["ipv4"]


def test_scrub_replaces_with_typed_placeholders() -> None:
    result = scrub("mail a@b.io, call +1 415-555-0100, key sk-abcdefghijklmnopqrstuv, ip fe80::1")
    assert result.text == "mail [EMAIL], call [PHONE], key [API_KEY], ip [IPV6]"
    assert result.counts == {"email": 1, "phone": 1, "api_key": 1, "ipv6": 1}
    assert not result.clean
    assert all(isinstance(d, Detection) for d in result.detections)


def test_scrub_clean_text_is_unchanged() -> None:
    text = "The quick brown fox, version 1.2.3, at 12:30, order 42."
    result = scrub(text)
    assert result.text == text
    assert result.clean
    assert result.counts == {}


def test_scrub_is_deterministic_over_seeded_corpus() -> None:
    rng = random.Random(1)  # noqa: S311
    words = ["alpha", "beta", "gamma", "a@b.io", "10.0.0.1", "+1 415-555-0100", "4111 1111 1111 1111"]
    for _ in range(20):
        text = " ".join(rng.choice(words) for _ in range(12))
        first = scrub(text)
        assert scrub(text) == first
        assert "@" not in first.text
        assert sum(first.counts.values()) == len(first.detections)


def test_kinds_cover_every_detector() -> None:
    assert set(KINDS) == {"email", "api_key", "credit_card", "ipv6", "ipv4", "phone"}

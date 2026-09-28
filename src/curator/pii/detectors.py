"""Detectors for PII and secrets in free text, plus a scrubber that redacts them.

Each detector is a pure function ``text -> list[Detection]``. :func:`detect`
runs them all in priority order and drops overlapping spans, and
:func:`scrub` replaces every detection with a typed placeholder such as
``[EMAIL]`` so a scrubbed record still reads sensibly.

The detectors are deliberately conservative: a phone number needs a leading
``+`` or at least one separator, a card number must pass the Luhn check, an
IPv6 candidate must parse with :mod:`ipaddress`, and a high-entropy token must
mix character classes and must not be a plain hex digest or UUID. The
false-positive guards are pinned by tests.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field

KINDS: tuple[str, ...] = ("email", "api_key", "credit_card", "ipv6", "ipv4", "phone")
"""Detector names in the priority order used to resolve overlapping spans."""


@dataclass(frozen=True, slots=True)
class Detection:
    """One matched span of PII or a secret."""

    kind: str
    start: int
    end: int
    text: str

    @property
    def placeholder(self) -> str:
        """The typed placeholder used when the span is scrubbed.

        >>> Detection("email", 0, 5, "a@b.io").placeholder
        '[EMAIL]'
        """
        return f"[{self.kind.upper()}]"


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """Scrubbed text plus what was removed from it."""

    text: str
    detections: tuple[Detection, ...]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """True when nothing was detected."""
        return not self.detections


_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")

# The outer lookarounds also reject a digit group on either side (with an optional separator),
# so a longer digit run such as a Luhn-failing 16-digit number is never split into a phone.
_PHONE_RE = re.compile(
    r"(?<![\w.+-])(?<!\d[\s.-])"
    r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]?\d{3,4}[\s.-]?\d{3,4}"
    r"(?![\w-])(?![\s.-]?\d)"
)

_IPV4_RE = re.compile(
    r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\w)(?!\.\d)"
)

_IPV6_RE = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")

_CARD_RE = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")

_KEY_PREFIX_RE = re.compile(
    r"(?<![\w-])(?:"
    r"sk-(?:ant-)?[A-Za-z0-9_-]{16,}"  # OpenAI / Anthropic style
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|AIza[0-9A-Za-z_-]{35}"  # Google API key
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")(?![\w-])"
)

_HIGH_ENTROPY_RE = re.compile(r"(?<![\w+/=-])[A-Za-z0-9+/_=-]{32,}(?![\w+/=-])")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+")
ENTROPY_THRESHOLD = 4.0
"""Bits per character below which a long token is not treated as a secret."""


def shannon_entropy(text: str) -> float:
    """Return the Shannon entropy of ``text`` in bits per character.

    >>> shannon_entropy("aaaa"), shannon_entropy("")
    (0.0, 0.0)
    >>> round(shannon_entropy("abcd"), 3)
    2.0
    """
    if not text:
        return 0.0
    counts = Counter(text)
    length = len(text)
    return abs(sum((n / length) * math.log2(n / length) for n in counts.values()))


def luhn_valid(digits: str) -> bool:
    """Return True when ``digits`` passes the Luhn checksum.

    >>> luhn_valid("4111111111111111")
    True
    >>> luhn_valid("4111111111111112"), luhn_valid("4111-1111")
    (False, False)
    """
    if not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _spans(pattern: re.Pattern[str], text: str, kind: str) -> list[Detection]:
    return [Detection(kind, m.start(), m.end(), m.group(0)) for m in pattern.finditer(text)]


def detect_emails(text: str) -> list[Detection]:
    """Email addresses."""
    return _spans(_EMAIL_RE, text, "email")


def detect_phones(text: str) -> list[Detection]:
    """Phone numbers with 10-15 digits and a leading ``+`` or at least one separator."""
    found = []
    for match in _PHONE_RE.finditer(text):
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        has_shape = raw.startswith("+") or any(c in raw for c in " .-()")
        if 10 <= len(digits) <= 15 and has_shape and len(set(digits)) > 1:
            found.append(Detection("phone", match.start(), match.end(), raw))
    return found


def detect_ipv4(text: str) -> list[Detection]:
    """Dotted-quad IPv4 addresses (version strings with a fifth part are skipped)."""
    return _spans(_IPV4_RE, text, "ipv4")


def detect_ipv6(text: str) -> list[Detection]:
    """IPv6 addresses that :mod:`ipaddress` accepts and that contain a digit."""
    found = []
    for match in _IPV6_RE.finditer(text):
        raw = match.group(0)
        if not any(c.isdigit() for c in raw):
            continue
        try:
            ipaddress.IPv6Address(raw)
        except ValueError:
            continue
        found.append(Detection("ipv6", match.start(), match.end(), raw))
    return found


def detect_credit_cards(text: str) -> list[Detection]:
    """13-19 digit numbers (spaces or dashes allowed) that pass the Luhn check."""
    found = []
    for match in _CARD_RE.finditer(text):
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if 13 <= len(digits) <= 19 and len(set(digits)) > 1 and luhn_valid(digits):
            found.append(Detection("credit_card", match.start(), match.end(), raw))
    return found


def _looks_like_digest(token: str) -> bool:
    stripped = token.replace("-", "")
    return _HEX_RE.fullmatch(stripped) is not None


def detect_api_keys(text: str, *, entropy_threshold: float = ENTROPY_THRESHOLD) -> list[Detection]:
    """Tokens with a known secret prefix, plus long high-entropy mixed tokens.

    Hex digests, UUIDs and tokens made of one character class are not secrets.
    """
    found = _spans(_KEY_PREFIX_RE, text, "api_key")
    taken = [(d.start, d.end) for d in found]
    for match in _HIGH_ENTROPY_RE.finditer(text):
        raw = match.group(0)
        if any(s < match.end() and match.start() < e for s, e in taken):
            continue
        has_alpha = any(c.isalpha() for c in raw)
        has_digit = any(c.isdigit() for c in raw)
        if not (has_alpha and has_digit) or _looks_like_digest(raw):
            continue
        if shannon_entropy(raw) < entropy_threshold:
            continue
        found.append(Detection("api_key", match.start(), match.end(), raw))
    return sorted(found, key=lambda d: d.start)


DETECTORS: dict[str, Callable[[str], list[Detection]]] = {
    "email": detect_emails,
    "api_key": detect_api_keys,
    "credit_card": detect_credit_cards,
    "ipv6": detect_ipv6,
    "ipv4": detect_ipv4,
    "phone": detect_phones,
}


def detect(text: str, kinds: Iterable[str] = KINDS) -> list[Detection]:
    """Run the named detectors in priority order and drop overlapping spans.

    >>> [d.kind for d in detect("mail a@b.io or call +1 415-555-0100")]
    ['email', 'phone']
    """
    found: list[Detection] = []
    for kind in kinds:
        if kind not in DETECTORS:
            msg = f"unknown detector {kind!r}; expected one of {', '.join(KINDS)}"
            raise ValueError(msg)
        for candidate in DETECTORS[kind](text):
            if any(d.start < candidate.end and candidate.start < d.end for d in found):
                continue
            found.append(candidate)
    return sorted(found, key=lambda d: d.start)


def scrub(text: str, kinds: Iterable[str] = KINDS) -> ScrubResult:
    """Replace every detection with its placeholder and count them per kind.

    >>> scrub("ping a@b.io from 10.0.0.1").text
    'ping [EMAIL] from [IPV4]'
    """
    detections = detect(text, kinds)
    pieces: list[str] = []
    cursor = 0
    for d in detections:
        pieces.append(text[cursor : d.start])
        pieces.append(d.placeholder)
        cursor = d.end
    pieces.append(text[cursor:])
    counts = dict(Counter(d.kind for d in detections))
    return ScrubResult("".join(pieces), tuple(detections), counts)

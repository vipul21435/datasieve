"""PII and secret detection primitives: typed detectors and a placeholder scrubber.

Like :mod:`curator.dedup`, these are usable on their own; the ``scrub``
pipeline stage will compose them.
"""

from curator.pii.detectors import DETECTORS
from curator.pii.detectors import ENTROPY_THRESHOLD
from curator.pii.detectors import KINDS
from curator.pii.detectors import Detection
from curator.pii.detectors import ScrubResult
from curator.pii.detectors import detect
from curator.pii.detectors import detect_api_keys
from curator.pii.detectors import detect_credit_cards
from curator.pii.detectors import detect_emails
from curator.pii.detectors import detect_ipv4
from curator.pii.detectors import detect_ipv6
from curator.pii.detectors import detect_phones
from curator.pii.detectors import luhn_valid
from curator.pii.detectors import scrub
from curator.pii.detectors import shannon_entropy

__all__ = [
    "DETECTORS",
    "ENTROPY_THRESHOLD",
    "KINDS",
    "Detection",
    "ScrubResult",
    "detect",
    "detect_api_keys",
    "detect_credit_cards",
    "detect_emails",
    "detect_ipv4",
    "detect_ipv6",
    "detect_phones",
    "luhn_valid",
    "scrub",
    "shannon_entropy",
]

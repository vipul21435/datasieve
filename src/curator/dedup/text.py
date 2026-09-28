"""Text normalisation and content hashing for exact deduplication.

Exact duplicates are records whose selected text fields are identical after
normalisation: lower-casing, whitespace collapsing and (optionally) dropping
punctuation. The fields are normalised one by one and joined with newlines,
so ``prompt="a b", response="c"`` and ``prompt="a", response="b c"`` never
collide.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import regex as re
import xxhash

from curator.schemas.fields import TextField

HashAlgorithm = Literal["xxhash", "sha256"]
"""``xxhash`` is a 128-bit xxh3 digest (fast); ``sha256`` is the cryptographic hash."""

# Unicode punctuation (P*) and symbols (S*): quotes, dashes, currency signs, emoji, ...
_PUNCTUATION = re.compile(r"[\p{P}\p{S}]+")


def normalize_text(
    text: str,
    *,
    lowercase: bool = True,
    collapse_whitespace: bool = True,
    strip_punctuation: bool = False,
) -> str:
    """The canonical form of ``text`` for exact matching.

    >>> normalize_text("  Hello,   WORLD!\\n")
    'hello, world!'
    >>> normalize_text("Hello, world!", strip_punctuation=True)
    'hello world'
    """
    if lowercase:
        text = text.lower()
    if strip_punctuation:
        text = _PUNCTUATION.sub("", text)
    if collapse_whitespace:
        text = " ".join(text.split())
    return text


@dataclass(frozen=True, slots=True)
class TextNormalizer:
    """:func:`normalize_text` with fixed options, plus field joining."""

    lowercase: bool = True
    collapse_whitespace: bool = True
    strip_punctuation: bool = False

    def __call__(self, text: str) -> str:
        return normalize_text(
            text,
            lowercase=self.lowercase,
            collapse_whitespace=self.collapse_whitespace,
            strip_punctuation=self.strip_punctuation,
        )

    def join(self, texts: Mapping[TextField, str], fields: Sequence[TextField]) -> str:
        """The selected fields, each normalised, one per line.

        >>> TextNormalizer().join({"prompt": "Hi  THERE", "response": "Hello"}, ["prompt", "response"])
        'hi there\\nhello'
        """
        return "\n".join(self(texts[field]) for field in fields)


def content_hash(text: str, algorithm: HashAlgorithm = "xxhash") -> str:
    """Hex digest of ``text`` (32 characters for ``xxhash``, 64 for ``sha256``).

    >>> content_hash("hello world")
    'df8d09e93f874900a99b8775cc15b6c7'
    >>> content_hash("hello world", "sha256")[:16]
    'b94d27b9934d3e08'
    """
    data = text.encode("utf-8", "surrogatepass")
    if algorithm == "xxhash":
        return xxhash.xxh3_128_hexdigest(data)
    return hashlib.sha256(data).hexdigest()


__all__ = ["HashAlgorithm", "TextNormalizer", "content_hash", "normalize_text"]

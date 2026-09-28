"""Text normalisation and content hashing."""

import pytest

from curator.dedup import TextNormalizer
from curator.dedup import content_hash
from curator.dedup import normalize_text

# Non-ASCII test data, spelled out so the source stays ASCII.
CAFE = "caf" + chr(0xE9)
EM_DASH = chr(0x2014)
CURLY_QUOTES = (chr(0x201C), chr(0x201D))
EURO = chr(0x20AC)


@pytest.mark.parametrize(
    ("text", "options", "expected"),
    [
        ("  Hello,\tWORLD!\r\n", {}, "hello, world!"),
        ("Hello, World!", {"lowercase": False}, "Hello, World!"),
        ("a  b", {"collapse_whitespace": False}, "a  b"),
        ('Don\'t stop -- "ever" (please)!', {"strip_punctuation": True}, "dont stop ever please"),
        (
            CAFE + " " + EM_DASH + " " + CURLY_QUOTES[0] + "quoted" + CURLY_QUOTES[1] + " " + EURO + "5",
            {"strip_punctuation": True},
            CAFE + " quoted 5",
        ),
        ("", {}, ""),
        ("\n \t", {}, ""),
    ],
)
def test_normalize_text(text: str, options: dict[str, bool], expected: str) -> None:
    assert normalize_text(text, **options) == expected


def test_normalizer_joins_fields_one_per_line_so_boundaries_survive() -> None:
    normalizer = TextNormalizer()
    left = normalizer.join({"prompt": "a b", "response": "c"}, ["prompt", "response"])
    right = normalizer.join({"prompt": "a", "response": "b c"}, ["prompt", "response"])
    assert left == "a b\nc"
    assert right == "a\nb c"
    assert normalizer.join({"prompt": "Only  THIS", "response": "no"}, ["prompt"]) == "only this"


def test_normalizer_applies_its_options() -> None:
    normalizer = TextNormalizer(lowercase=False, strip_punctuation=True)
    assert normalizer("Hi, There!") == "Hi There"


@pytest.mark.parametrize(("algorithm", "length"), [("xxhash", 32), ("sha256", 64)])
def test_content_hash_is_stable_and_hex(algorithm: str, length: int) -> None:
    digest = content_hash("hello world", algorithm)  # type: ignore[arg-type]
    assert len(digest) == length
    assert int(digest, 16) >= 0
    assert digest == content_hash("hello world", algorithm)  # type: ignore[arg-type]
    assert digest != content_hash("hello world!", algorithm)  # type: ignore[arg-type]


def test_content_hash_survives_lone_surrogates() -> None:
    assert content_hash("bad \ud800 text") != content_hash("bad  text")

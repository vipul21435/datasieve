"""MinHash signatures and banded LSH for near-duplicate lookup.

The signature scheme is text_dedup's 64-bit MinHash configuration (xxh3 base
hash, ``(a * x + b) mod (2^61 - 1)`` permutations, 32-bit minimums), and the
band/row split comes from its ``optimal_param``, so a threshold means the same
thing here as in ``text_dedup.minhash``. Candidates found through LSH are
verified with the exact Jaccard similarity of their word n-gram sets, so the
similarity a caller sees is never an estimate.

:class:`NearDuplicateIndex` is the keep-first lookup the dedup stage uses:
fingerprint a text, ``lookup`` the best indexed match at or above the
threshold, and ``add`` the text when it is new.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from typing import cast

import numpy as np
import numpy.typing as npt
import regex as re

from text_dedup.config.algorithms.minhash import optimal_param
from text_dedup.utils.hashfunc import xxh3_hash
from text_dedup.utils.tokenization import ngrams

# text_dedup's 64-bit configuration: 32-bit hashes permuted modulo a Mersenne prime.
MAX_HASH = np.uint64((1 << 32) - 1)
MODULO_PRIME = np.uint64((1 << 61) - 1)

Signature = npt.NDArray[np.uint64]
Shingles = frozenset[int]

_NON_WORD = re.compile(r"\W+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens: maximal runs of word characters.

    >>> tokenize("Hello, World!  It's 5pm.")
    ['hello', 'world', 'it', 's', '5pm']
    """
    return [token for token in _NON_WORD.split(text.lower()) if token]


def shingles(text: str, ngram_size: int, *, seed: int = 42) -> Shingles:
    """32-bit hashes of the word n-grams of ``text``; a text shorter than ``ngram_size`` words is one n-gram.

    >>> len(shingles("a b c d", 3))
    2
    >>> len(shingles("a b", 3)), len(shingles("", 3))
    (1, 0)
    """
    tokens = tokenize(text)
    return frozenset(
        xxh3_hash(" ".join(gram).encode("utf-8", "surrogatepass"), seed=seed, bits=32)
        for gram in ngrams(tokens, ngram_size, min_length=1)
    )


def jaccard(a: Shingles, b: Shingles) -> float:
    """Exact Jaccard similarity; two empty sets count as identical.

    >>> jaccard(frozenset({1, 2, 3}), frozenset({2, 3, 4}))
    0.5
    """
    union = len(a | b)
    return len(a & b) / union if union else 1.0


@cache
def lsh_params(threshold: float, num_perm: int) -> tuple[int, int]:
    """``(bands, rows)`` minimising the false positive plus false negative area at ``threshold``.

    >>> lsh_params(0.8, 128)
    (9, 13)
    """
    return optimal_param(threshold, num_perm)


class MinHasher:
    """Turns shingle sets into fixed-length signatures; one seed always gives the same permutations."""

    def __init__(self, *, num_perm: int = 128, seed: int = 42) -> None:
        self.num_perm = num_perm
        self.seed = seed
        rng = np.random.RandomState(seed)
        self._a = cast(Signature, rng.randint(1, MODULO_PRIME, size=(num_perm,), dtype=np.uint64))
        self._b = cast(Signature, rng.randint(0, MODULO_PRIME, size=(num_perm,), dtype=np.uint64))

    def signature(self, shingle_set: Shingles) -> Signature:
        """The ``num_perm`` minimums; an empty set maps to all ``MAX_HASH``."""
        if not shingle_set:
            return np.full(self.num_perm, MAX_HASH, dtype=np.uint64)
        values = np.fromiter(shingle_set, dtype=np.uint64, count=len(shingle_set)).reshape(-1, 1)
        # Universal hashing as in text_dedup; uint64 arithmetic wraps on overflow, which is fine for a hash.
        permuted = (values * self._a + self._b) % MODULO_PRIME & MAX_HASH
        return cast(Signature, permuted.min(axis=0))


def estimate_similarity(a: Signature, b: Signature) -> float:
    """The fraction of equal minimums: an unbiased estimate of the Jaccard similarity."""
    return float(np.mean(a == b))


class LSHIndex:
    """Banded LSH: two signatures are candidates when any band of ``rows`` values is identical."""

    def __init__(self, *, num_perm: int, threshold: float, bands: int | None = None, rows: int | None = None) -> None:
        if bands is None or rows is None:
            bands, rows = lsh_params(threshold, num_perm)
        if bands < 1 or rows < 1 or bands * rows > num_perm:
            raise ValueError(f"bands * rows must be within 1..{num_perm}, got {bands} * {rows}")
        self.bands = bands
        self.rows = rows
        self._buckets: dict[tuple[int, bytes], list[int]] = {}

    def _keys(self, signature: Signature) -> Iterator[tuple[int, bytes]]:
        for band in range(self.bands):
            start = band * self.rows
            yield band, signature[start : start + self.rows].tobytes()

    def insert(self, item: int, signature: Signature) -> None:
        for key in self._keys(signature):
            self._buckets.setdefault(key, []).append(item)

    def query(self, signature: Signature) -> list[int]:
        """Items sharing at least one band with ``signature``, each once, in ascending order."""
        return sorted({item for key in self._keys(signature) for item in self._buckets.get(key, ())})


@dataclass(frozen=True, slots=True, eq=False)
class Fingerprint:
    """What the index keeps about a text: its shingles (for verification) and its signature (for LSH)."""

    shingles: Shingles
    signature: Signature


@dataclass(frozen=True, slots=True)
class Match:
    key: str
    similarity: float


class NearDuplicateIndex:
    """Keep-first near-duplicate lookup over texts identified by string keys.

    :meth:`lookup` returns the most similar indexed text whose Jaccard
    similarity is at least ``threshold`` (the earliest added one on ties), or
    ``None``. Texts are numbered in the order they are added.
    """

    def __init__(self, *, num_perm: int = 128, ngram_size: int = 3, threshold: float = 0.8, seed: int = 42) -> None:
        self.ngram_size = ngram_size
        self.threshold = threshold
        self.seed = seed
        self._hasher = MinHasher(num_perm=num_perm, seed=seed)
        self._lsh = LSHIndex(num_perm=num_perm, threshold=threshold)
        self._keys: list[str] = []
        self._shingles: list[Shingles] = []

    @property
    def bands(self) -> int:
        return self._lsh.bands

    @property
    def rows(self) -> int:
        return self._lsh.rows

    def __len__(self) -> int:
        return len(self._keys)

    def fingerprint(self, text: str) -> Fingerprint:
        shingle_set = shingles(text, self.ngram_size, seed=self.seed)
        return Fingerprint(shingle_set, self._hasher.signature(shingle_set))

    def lookup(self, fingerprint: Fingerprint) -> Match | None:
        best: Match | None = None
        for item in self._lsh.query(fingerprint.signature):
            similarity = jaccard(fingerprint.shingles, self._shingles[item])
            if similarity >= self.threshold and (best is None or similarity > best.similarity):
                best = Match(self._keys[item], similarity)
        return best

    def add(self, key: str, fingerprint: Fingerprint) -> None:
        self._lsh.insert(len(self._keys), fingerprint.signature)
        self._keys.append(key)
        self._shingles.append(fingerprint.shingles)


__all__ = [
    "MAX_HASH",
    "MODULO_PRIME",
    "Fingerprint",
    "LSHIndex",
    "Match",
    "MinHasher",
    "NearDuplicateIndex",
    "Shingles",
    "Signature",
    "estimate_similarity",
    "jaccard",
    "lsh_params",
    "shingles",
    "tokenize",
]

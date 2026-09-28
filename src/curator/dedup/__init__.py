"""Duplicate detection primitives: text normalisation, content hashing and MinHash/LSH lookup.

The ``dedup`` pipeline stage (:mod:`curator.stages.dedup`) composes these;
they are also usable on their own, for example to check a synthetic batch
against an evaluation set.
"""

from curator.dedup.minhash import Fingerprint
from curator.dedup.minhash import LSHIndex
from curator.dedup.minhash import Match
from curator.dedup.minhash import MinHasher
from curator.dedup.minhash import NearDuplicateIndex
from curator.dedup.minhash import estimate_similarity
from curator.dedup.minhash import jaccard
from curator.dedup.minhash import lsh_params
from curator.dedup.minhash import shingles
from curator.dedup.minhash import tokenize
from curator.dedup.text import HashAlgorithm
from curator.dedup.text import TextNormalizer
from curator.dedup.text import content_hash
from curator.dedup.text import normalize_text

__all__ = [
    "Fingerprint",
    "HashAlgorithm",
    "LSHIndex",
    "Match",
    "MinHasher",
    "NearDuplicateIndex",
    "TextNormalizer",
    "content_hash",
    "estimate_similarity",
    "jaccard",
    "lsh_params",
    "normalize_text",
    "shingles",
    "tokenize",
]

"""MinHash signatures, LSH banding and the keep-first near-duplicate index."""

import numpy as np
import pytest

from curator.dedup import LSHIndex
from curator.dedup import MinHasher
from curator.dedup import NearDuplicateIndex
from curator.dedup import estimate_similarity
from curator.dedup import jaccard
from curator.dedup import lsh_params
from curator.dedup import shingles
from curator.dedup import tokenize
from curator.dedup.minhash import MAX_HASH

WORDS = [f"w{i}" for i in range(30)]
BASE = " ".join(WORDS)
ONE_WORD_CHANGED = " ".join("x" if i == 15 else w for i, w in enumerate(WORDS))
TWO_WORDS_CHANGED = " ".join("x" if i in (8, 22) else w for i, w in enumerate(WORDS))
UNRELATED = " ".join(f"u{i}" for i in range(30))


def test_tokenize_splits_on_non_word_characters_and_lower_cases() -> None:
    assert tokenize("Hello, World!  It's 5pm.") == ["hello", "world", "it", "s", "5pm"]
    assert tokenize("...") == []
    accented = "caf" + chr(0xE9) + " na" + chr(0xEF) + "ve"
    assert tokenize(accented) == accented.split()


def test_shingles_are_word_ngrams() -> None:
    assert len(shingles("a b c d e", 3)) == 3
    assert len(shingles("a b", 3)) == 1  # shorter than one n-gram: the whole text is the n-gram
    assert shingles("", 3) == frozenset()
    assert shingles("a b c", 2) == shingles("A, b; C!", 2)
    assert shingles("a b c", 2, seed=1) != shingles("a b c", 2, seed=2)


def test_jaccard_of_planted_edits() -> None:
    base = shingles(BASE, 3)
    # One changed word touches three 3-grams: 25 shared out of 31; two changed words: 22 out of 34.
    assert jaccard(base, shingles(ONE_WORD_CHANGED, 3)) == pytest.approx(25 / 31)
    assert jaccard(base, shingles(TWO_WORDS_CHANGED, 3)) == pytest.approx(22 / 34)
    assert jaccard(base, shingles(UNRELATED, 3)) == 0.0
    assert jaccard(frozenset(), frozenset()) == 1.0


def test_signatures_are_deterministic_per_seed() -> None:
    shingle_set = shingles(BASE, 3)
    first = MinHasher(num_perm=64, seed=7).signature(shingle_set)
    second = MinHasher(num_perm=64, seed=7).signature(shingle_set)
    other_seed = MinHasher(num_perm=64, seed=8).signature(shingle_set)

    assert first.dtype == np.uint64
    assert first.shape == (64,)
    assert np.array_equal(first, second)
    assert not np.array_equal(first, other_seed)
    assert np.all(first <= MAX_HASH)


def test_empty_text_has_the_all_max_signature() -> None:
    signature = MinHasher(num_perm=16).signature(frozenset())
    assert np.all(signature == MAX_HASH)


def test_estimate_tracks_the_exact_similarity() -> None:
    hasher = MinHasher(num_perm=256)
    base = hasher.signature(shingles(BASE, 3))
    near = hasher.signature(shingles(ONE_WORD_CHANGED, 3))
    far = hasher.signature(shingles(UNRELATED, 3))
    assert estimate_similarity(base, base) == 1.0
    assert abs(estimate_similarity(base, near) - 25 / 31) < 0.1
    assert estimate_similarity(base, far) < 0.1


@pytest.mark.parametrize(("threshold", "num_perm"), [(0.5, 32), (0.8, 128), (0.95, 128)])
def test_lsh_params_fit_within_the_signature(threshold: float, num_perm: int) -> None:
    bands, rows = lsh_params(threshold, num_perm)
    assert bands >= 1 and rows >= 1
    assert bands * rows <= num_perm


def test_lsh_index_returns_items_sharing_a_band_once_each_in_order() -> None:
    index = LSHIndex(num_perm=8, threshold=0.5, bands=4, rows=2)
    index.insert(0, np.arange(8, dtype=np.uint64))
    index.insert(1, np.arange(8, dtype=np.uint64))
    index.insert(2, np.arange(8, 16, dtype=np.uint64))

    assert index.query(np.arange(8, dtype=np.uint64)) == [0, 1]
    assert index.query(np.array([0, 1, 9, 9, 9, 9, 8, 9], dtype=np.uint64)) == [0, 1]
    assert index.query(np.array([9, 9, 9, 9, 9, 9, 14, 15], dtype=np.uint64)) == [2]
    assert index.query(np.full(8, 99, dtype=np.uint64)) == []


def test_lsh_index_rejects_bands_that_do_not_fit() -> None:
    with pytest.raises(ValueError, match="bands \\* rows"):
        LSHIndex(num_perm=8, threshold=0.5, bands=3, rows=3)


def test_near_duplicate_index_finds_the_best_match_above_the_threshold() -> None:
    index = NearDuplicateIndex(num_perm=128, ngram_size=3, threshold=0.7, seed=42)
    index.add("base", index.fingerprint(BASE))
    index.add("other", index.fingerprint(UNRELATED))

    match = index.lookup(index.fingerprint(ONE_WORD_CHANGED))
    assert match is not None
    assert (match.key, match.similarity) == ("base", pytest.approx(25 / 31))
    assert index.lookup(index.fingerprint(TWO_WORDS_CHANGED)) is None  # 0.647 is below 0.7
    assert index.lookup(index.fingerprint("something else entirely")) is None
    assert len(index) == 2


def test_near_duplicate_index_threshold_is_inclusive_and_configurable() -> None:
    index = NearDuplicateIndex(num_perm=128, ngram_size=3, threshold=22 / 34, seed=42)
    index.add("base", index.fingerprint(BASE))
    match = index.lookup(index.fingerprint(TWO_WORDS_CHANGED))
    assert match is not None
    assert match.key == "base"


def test_near_duplicate_index_prefers_the_earliest_of_equal_matches() -> None:
    index = NearDuplicateIndex(num_perm=64, threshold=0.5)
    index.add("first", index.fingerprint(BASE))
    index.add("second", index.fingerprint(BASE))
    match = index.lookup(index.fingerprint(BASE))
    assert match is not None
    assert (match.key, match.similarity) == ("first", 1.0)


def test_near_duplicate_index_uses_text_dedup_banding() -> None:
    index = NearDuplicateIndex(num_perm=128, threshold=0.8)
    assert (index.bands, index.rows) == lsh_params(0.8, 128) == (9, 13)

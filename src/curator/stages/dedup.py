"""The ``dedup`` stage: drop exact duplicates, near duplicates and records that overlap a reference set.

The input is the ``validate`` stage's output, one valid record per line. Each
record's text fields (``fields``; by default every text field of the record
kind, see :data:`curator.schemas.fields.TEXT_FIELDS`) are normalised and
joined into one text, which is checked in this order, keep-first:

1. ``contaminated``: the text matches a record of ``reference.path`` (an
   evaluation set, say) exactly, or with a Jaccard similarity of word n-grams
   at or above ``reference.threshold`` (default: ``near.threshold``);
2. ``exact_duplicate``: the hash of the normalised text was already kept;
3. ``near_duplicate``: a kept record's n-gram set is at least
   ``near.threshold`` similar. MinHash/LSH proposes the candidates and the
   exact Jaccard similarity decides, so the reported similarity is never an
   estimate. Banded LSH proposes a pair sitting right at the threshold with
   only even odds (the usual S-curve), so set the threshold a little below
   the similarity that must be caught.

The first record of a group is kept and every later member is dropped, so the
output keeps the input's order, and a re-run (same seed, same input) gives
byte-identical files. Groups are stars around the kept record: a record that
resembles a *dropped* record but not the kept one is kept.

Outputs, all written atomically into the run's output directory:

* ``output_file`` (``deduped.jsonl``): the kept records, unchanged;
* ``rejects_file`` (``dedup_rejects.jsonl``): one entry per dropped record::

    {"line": 9, "id": "sft-009", "reason": "near_duplicate", "match": "sft-003",
     "similarity": 0.8125, "record": {...the input record...}}

  ``match`` is the id of the kept record, or of the reference record for
  ``contaminated``; ``similarity`` is 1.0 for exact matches;
* ``clusters_file`` (``dedup_clusters.jsonl``): one entry per kept record that
  had duplicates::

    {"kept": "sft-003", "size": 3, "members": [{"id": "sft-009", "reason": "near_duplicate", "similarity": 0.8125}]}

  Contaminated records are not clustered, since nothing of theirs is kept.

A reference-set line may be a record of the pipeline's kind or a plain object
with the compared fields as strings; its id is the line's ``id`` when it has
one, else ``line:<number>``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Final
from typing import Literal

from curator.config.spec import DedupStageSpec
from curator.config.spec import NearDuplicateSpec
from curator.dedup.minhash import Fingerprint
from curator.dedup.minhash import NearDuplicateIndex
from curator.dedup.text import HashAlgorithm
from curator.dedup.text import TextNormalizer
from curator.dedup.text import content_hash
from curator.errors import ConfigError
from curator.errors import RecordValidationError
from curator.errors import StageInputError
from curator.io.jsonl import AtomicFile
from curator.io.jsonl import StrictJSONError
from curator.io.jsonl import encode_json_line_lossless
from curator.io.jsonl import loads_strict
from curator.io.jsonl import read_lines
from curator.log import log_context
from curator.schemas.fields import TextField
from curator.schemas.fields import record_texts
from curator.schemas.parse import Record
from curator.schemas.parse import RecordKind
from curator.schemas.parse import parse_record

STAGE: Final = "dedup"

logger = logging.getLogger(__name__)

DedupReason = Literal["exact_duplicate", "near_duplicate", "contaminated"]
DEDUP_REASONS: Final[tuple[DedupReason, ...]] = ("exact_duplicate", "near_duplicate", "contaminated")


# ---------------------------------------------------------------------------
# Content index: exact hash first, MinHash/LSH second


@dataclass(frozen=True, slots=True, eq=False)
class Probe:
    """What the index needs to compare one text: its digest and, with near matching on, its fingerprint."""

    digest: str
    fingerprint: Fingerprint | None


@dataclass(frozen=True, slots=True)
class Hit:
    """An indexed text that matches: its key, the similarity and whether it was an exact match."""

    key: str
    similarity: float
    exact: bool


class ContentIndex:
    """Keep-first lookup of texts by exact hash and, optionally, by MinHash/LSH similarity.

    An exact match wins (similarity 1.0); otherwise :meth:`lookup` returns the
    most similar indexed text at or above ``threshold`` (default:
    ``near.threshold``), the earliest added one on ties. Without ``near``
    only exact matches are found. Fingerprints only depend on ``near``'s
    ``num_perm``, ``ngram_size`` and ``seed``, so two indexes built from the
    same :class:`~curator.config.spec.NearDuplicateSpec` can share probes.
    """

    def __init__(
        self,
        *,
        algorithm: HashAlgorithm = "xxhash",
        near: NearDuplicateSpec | None = None,
        threshold: float | None = None,
    ) -> None:
        self.algorithm = algorithm
        self._digests: dict[str, str] = {}
        self._near: NearDuplicateIndex | None = None
        if near is not None:
            self._near = NearDuplicateIndex(
                num_perm=near.num_perm,
                ngram_size=near.ngram_size,
                threshold=threshold if threshold is not None else near.threshold,
                seed=near.seed,
            )
        self._size = 0

    def __len__(self) -> int:
        return self._size

    @property
    def near_enabled(self) -> bool:
        return self._near is not None

    def probe(self, text: str) -> Probe:
        fingerprint = self._near.fingerprint(text) if self._near is not None else None
        return Probe(content_hash(text, self.algorithm), fingerprint)

    def lookup(self, probe: Probe) -> Hit | None:
        key = self._digests.get(probe.digest)
        if key is not None:
            return Hit(key, 1.0, exact=True)
        if self._near is not None and probe.fingerprint is not None:
            match = self._near.lookup(probe.fingerprint)
            if match is not None:
                return Hit(match.key, match.similarity, exact=False)
        return None

    def add(self, key: str, probe: Probe) -> None:
        """Index ``probe`` under ``key``; a digest that is already indexed keeps its first key."""
        if self._near is not None:
            if probe.fingerprint is None:
                raise ValueError("the probe has no fingerprint; it was made by an index without near matching")
            self._near.add(key, probe.fingerprint)
        self._digests.setdefault(probe.digest, key)
        self._size += 1


# ---------------------------------------------------------------------------
# Per-record decisions


@dataclass(frozen=True, slots=True)
class Duplicate:
    """Why a record is dropped: the reason, the id it matched and how similar they are."""

    reason: DedupReason
    match: str
    similarity: float


def _normalizer(config: DedupStageSpec) -> TextNormalizer:
    return TextNormalizer(
        lowercase=config.normalize.lowercase,
        collapse_whitespace=config.normalize.collapse_whitespace,
        strip_punctuation=config.normalize.strip_punctuation,
    )


class Deduplicator:
    """Keep-first duplicate detection over typed records, fed in input order.

    :meth:`check` returns why a record is a duplicate, or ``None`` and then
    remembers the record as kept. ``reference`` is the index of the set the
    input must not overlap with (see :func:`load_reference`).
    """

    def __init__(self, *, kind: RecordKind, config: DedupStageSpec, reference: ContentIndex | None = None) -> None:
        self.kind = kind
        self.config = config
        self.fields = config.fields_for(kind)
        self.reference_fields = config.reference_fields_for(kind)
        self.normalizer = _normalizer(config)
        self.index = ContentIndex(algorithm=config.hash, near=config.near if config.near.enabled else None)
        self.reference = reference

    def check(self, record: Record) -> Duplicate | None:
        texts = record_texts(record)
        text = self.normalizer.join(texts, self.fields)
        probe = self.index.probe(text)
        if self.reference is not None:
            hit = self.reference.lookup(self._reference_probe(texts, text, probe))
            if hit is not None:
                return Duplicate("contaminated", hit.key, hit.similarity)
        hit = self.index.lookup(probe)
        if hit is not None:
            return Duplicate("exact_duplicate" if hit.exact else "near_duplicate", hit.key, hit.similarity)
        self.index.add(record.record_id, probe)
        return None

    def _reference_probe(self, texts: Mapping[TextField, str], text: str, probe: Probe) -> Probe:
        if self.reference is None:  # pragma: no cover - only called with a reference
            raise ValueError("no reference index")
        # Both indexes are built from the same near settings, so the fingerprint can be reused when it exists.
        if self.reference_fields == self.fields and (probe.fingerprint is not None or not self.reference.near_enabled):
            return probe
        return self.reference.probe(self.normalizer.join(texts, self.reference_fields))


# ---------------------------------------------------------------------------
# Reading input and reference lines


def _decode_object(content: bytes, *, line: int, path: Path | None) -> dict[str, object]:
    where = f"line {line}" if path is None else f"line {line} of {path}"
    try:
        decoded = loads_strict(content.decode("utf-8"))
    except UnicodeDecodeError:
        raise StageInputError(f"{STAGE}: {where} is not valid UTF-8", stage=STAGE, line=line, path=path) from None
    except (json.JSONDecodeError, StrictJSONError) as exc:
        raise StageInputError(f"{STAGE}: {where} is not valid JSON: {exc}", stage=STAGE, line=line, path=path) from None
    if not isinstance(decoded, dict):
        raise StageInputError(f"{STAGE}: {where} is not a JSON object", stage=STAGE, line=line, path=path)
    return decoded


def _reference_id(obj: Mapping[str, object], line: int) -> str:
    value = obj.get("id")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return f"line:{line}"


def _plain_texts(obj: Mapping[str, object], fields: Sequence[TextField]) -> dict[TextField, str] | None:
    texts: dict[TextField, str] = {}
    for name in fields:
        value = obj.get(name)
        if not isinstance(value, str):
            return None
        texts[name] = value
    return texts


def _reference_texts(
    obj: Mapping[str, object], *, kind: RecordKind, fields: Sequence[TextField], line: int, path: Path
) -> dict[TextField, str]:
    try:
        return record_texts(parse_record(obj, kind, unknown_fields="metadata"))
    except RecordValidationError:
        texts = _plain_texts(obj, fields)
    if texts is None:
        raise StageInputError(
            f"{STAGE}: line {line} of {path} is neither a {kind} record nor an object with string fields "
            f"{', '.join(fields)}",
            stage=STAGE,
            line=line,
            path=path,
        )
    return texts


def load_reference(path: Path, *, kind: RecordKind, config: DedupStageSpec) -> ContentIndex:
    """Index the reference set at ``path`` with the stage's fields, normalisation and thresholds.

    Raises :class:`~curator.errors.InputFileError` if the file cannot be read
    and :class:`~curator.errors.StageInputError` for a line that is neither a
    record of ``kind`` nor an object with the compared fields as strings.
    """
    threshold = config.reference.threshold if config.reference is not None else None
    index = ContentIndex(algorithm=config.hash, near=config.near, threshold=threshold)
    normalizer = _normalizer(config)
    fields = config.reference_fields_for(kind)
    with closing(read_lines(path)) as lines:
        for number, content in lines:
            if not content.strip():
                continue
            obj = _decode_object(content, line=number, path=path)
            texts = _reference_texts(obj, kind=kind, fields=fields, line=number, path=path)
            index.add(_reference_id(obj, number), index.probe(normalizer.join(texts, fields)))
    logger.info("dedup.reference_loaded", extra={"path": path, "records": len(index), "fields": list(fields)})
    return index


# ---------------------------------------------------------------------------
# Streaming over the input


@dataclass(frozen=True, slots=True)
class Kept:
    """A record that stays, as its unchanged input line."""

    line: int
    record_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Dropped:
    """A record removed as a duplicate or as contaminated."""

    line: int
    record_id: str
    reason: DedupReason
    match: str
    """Id of the kept record, or of the reference record for ``contaminated``."""
    similarity: float
    record: object

    def to_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "id": self.record_id,
            "reason": self.reason,
            "match": self.match,
            "similarity": round(self.similarity, 6),
            "record": self.record,
        }


Outcome = Kept | Dropped


def dedup_lines(
    lines: Iterable[tuple[int, bytes]],
    *,
    kind: RecordKind,
    config: DedupStageSpec,
    reference: ContentIndex | None = None,
    source: Path | None = None,
) -> Iterator[Outcome]:
    """Yield one outcome per non-blank ``(line_number, content)`` pair, in order.

    Every line must be a valid record of ``kind`` (the ``validate`` stage's
    output); anything else raises :class:`~curator.errors.StageInputError`.
    ``source`` only improves that error's message. No files, no logging.
    """
    deduplicator = Deduplicator(kind=kind, config=config, reference=reference)
    for number, content in lines:
        if not content.strip():
            continue
        decoded = _decode_object(content, line=number, path=source)
        try:
            record = parse_record(decoded, kind)
        except RecordValidationError as exc:
            where = f"line {number}" if source is None else f"line {number} of {source}"
            raise StageInputError(
                f"{STAGE}: {where} is not a valid {kind} record ({exc}); run the validate stage first",
                stage=STAGE,
                line=number,
                path=source,
            ) from None
        duplicate = deduplicator.check(record)
        if duplicate is None:
            yield Kept(number, record.record_id, content + b"\n")
        else:
            yield Dropped(number, record.record_id, duplicate.reason, duplicate.match, duplicate.similarity, decoded)


# ---------------------------------------------------------------------------
# The stage


@dataclass(frozen=True, slots=True)
class DedupReport:
    """What one run of the stage did."""

    input_path: Path
    output_path: Path
    rejects_path: Path
    clusters_path: Path
    total: int
    kept: int
    reasons: Mapping[str, int] = field(default_factory=dict)
    """Number of dropped records per reason code."""
    groups: int = 0
    """Number of duplicate groups (lines in the clusters file)."""
    reference_size: int | None = None
    """Records in the reference set, or ``None`` without a contamination check."""

    @property
    def dropped(self) -> int:
        return self.total - self.kept

    @property
    def dropped_fraction(self) -> float:
        return self.dropped / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": STAGE,
            "input": str(self.input_path),
            "output": str(self.output_path),
            "rejects": str(self.rejects_path),
            "clusters": str(self.clusters_path),
            "total": self.total,
            "kept": self.kept,
            "dropped": self.dropped,
            "dropped_fraction": round(self.dropped_fraction, 6),
            "reasons": dict(self.reasons),
            "groups": self.groups,
            "reference_size": self.reference_size,
        }


def _check_paths(protected: Sequence[Path], outputs: Sequence[Path]) -> None:
    sources = {path.resolve() for path in protected}
    for target in outputs:
        if target.resolve() in sources:
            raise ConfigError(
                f"{STAGE}: output {target} would overwrite the input or reference file", details={"path": str(target)}
            )


def run_dedup(
    input_path: Path,
    output_dir: Path,
    *,
    kind: RecordKind,
    config: DedupStageSpec | None = None,
) -> DedupReport:
    """Deduplicate ``input_path`` and write the kept, rejected and cluster files into ``output_dir``.

    Raises :class:`~curator.errors.InputFileError` if the input or the
    reference set cannot be read, :class:`~curator.errors.StageInputError`
    for a line that is not a valid record, and
    :class:`~curator.errors.ConfigError` if an output would overwrite the
    input or the reference file.
    """
    config = config if config is not None else DedupStageSpec()
    output_path = output_dir / config.output_file
    rejects_path = output_dir / config.rejects_file
    clusters_path = output_dir / config.clusters_file
    protected = [input_path] if config.reference is None else [input_path, config.reference.path]
    _check_paths(protected, (output_path, rejects_path, clusters_path))

    with log_context(stage=STAGE):
        reference = None
        if config.reference is not None:
            reference = load_reference(config.reference.path, kind=kind, config=config)
        with closing(read_lines(input_path)) as lines:
            output_dir.mkdir(parents=True, exist_ok=True)
            total = kept = 0
            reasons: Counter[str] = Counter()
            clusters: dict[str, list[dict[str, object]]] = {}
            with (
                AtomicFile(output_path) as output_file,
                AtomicFile(rejects_path) as rejects_file,
                AtomicFile(clusters_path) as clusters_file,
            ):
                for outcome in dedup_lines(lines, kind=kind, config=config, reference=reference, source=input_path):
                    total += 1
                    if isinstance(outcome, Kept):
                        kept += 1
                        output_file.write(outcome.data)
                        continue
                    reasons[outcome.reason] += 1
                    entry = outcome.to_dict()
                    rejects_file.write(encode_json_line_lossless(entry))
                    if outcome.reason != "contaminated":
                        member = {"id": outcome.record_id, "reason": outcome.reason, "similarity": entry["similarity"]}
                        clusters.setdefault(outcome.match, []).append(member)
                    logger.debug("dedup.record_dropped", extra={key: entry[key] for key in entry if key != "record"})
                for kept_id, members in clusters.items():
                    group = {"kept": kept_id, "size": len(members) + 1, "members": members}
                    clusters_file.write(encode_json_line_lossless(group))

                report = DedupReport(
                    input_path=input_path,
                    output_path=output_path,
                    rejects_path=rejects_path,
                    clusters_path=clusters_path,
                    total=total,
                    kept=kept,
                    reasons=dict(sorted(reasons.items())),
                    groups=len(clusters),
                    reference_size=len(reference) if reference is not None else None,
                )
                output_file.commit()
                rejects_file.commit()
                clusters_file.commit()
            if total == 0:
                logger.warning("dedup.empty_input", extra={"input": input_path})
            logger.info("dedup.finished", extra=report.to_dict())
    return report


__all__ = [
    "DEDUP_REASONS",
    "STAGE",
    "ContentIndex",
    "DedupReason",
    "DedupReport",
    "Deduplicator",
    "Dropped",
    "Duplicate",
    "Hit",
    "Kept",
    "Outcome",
    "Probe",
    "dedup_lines",
    "load_reference",
    "run_dedup",
]

"""The ``validate`` stage: split a JSONL file into valid records and a quarantine file.

Every non-blank input line ends up in exactly one of two outputs:

* ``output_file`` gets each valid record, re-serialised with its ``id``
  filled in (content-derived when the input had none) and otherwise in its
  input shape;
* ``quarantine_file`` gets one entry per rejected line, with every reason::

    {"line": 7, "id": "ex-7", "reasons": [{"code": "blank_text", "field": "messages[1].content",
     "message": "must contain non-whitespace text"}], "record": {...the input record...}}

  Lines that are not JSON carry ``"raw"`` (the line text) instead of ``"record"``.

Reason codes are stable (see :mod:`curator.schemas.issues`). On top of the
schema codes this stage adds ``invalid_encoding``, ``invalid_json``,
``duplicate_key``, ``non_finite_number``, ``invalid_unicode`` and
``duplicate_id``.

Both files are written atomically. Re-running on the same input yields
byte-identical outputs. When more than ``max_invalid_fraction`` of the records
is quarantined the stage raises :class:`~curator.errors.QuarantineThresholdError`;
the quarantine file is still written for inspection, but ``output_file`` is
not (and a stale copy from an earlier run is removed), so nothing downstream
consumes a half-good dataset.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Final

from curator.config.spec import ValidateStageSpec
from curator.errors import ConfigError
from curator.errors import QuarantineThresholdError
from curator.errors import RecordValidationError
from curator.io.jsonl import AtomicFile
from curator.io.jsonl import StrictJSONError
from curator.io.jsonl import encode_json_line
from curator.io.jsonl import encode_json_line_lossless
from curator.io.jsonl import loads_strict
from curator.io.jsonl import read_lines
from curator.log import log_context
from curator.schemas.issues import RecordIssue
from curator.schemas.parse import RecordKind
from curator.schemas.parse import parse_record

STAGE: Final = "validate"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Accepted:
    """A valid record, already serialised as one output line."""

    line: int
    record_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Quarantined:
    """A rejected line and every reason it was rejected."""

    line: int
    issues: tuple[RecordIssue, ...]
    record: object = None
    """The decoded JSON value, when the line was valid JSON."""
    raw: str | None = None
    """The line text, when it could not be decoded as JSON."""

    def to_dict(self) -> dict[str, object]:
        entry: dict[str, object] = {"line": self.line}
        if isinstance(self.record, dict) and "id" in self.record:
            entry["id"] = self.record["id"]
        entry["reasons"] = [issue.to_dict() for issue in self.issues]
        if self.raw is not None:
            entry["raw"] = self.raw
        else:
            entry["record"] = self.record
        return entry


Outcome = Accepted | Quarantined


def _decode(line: int, content: bytes) -> object | Quarantined:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        issue = RecordIssue(
            "invalid_encoding", "", f"line is not valid UTF-8: byte 0x{content[exc.start]:02x} at offset {exc.start}"
        )
        return Quarantined(line, (issue,), raw=content.decode("utf-8", "backslashreplace"))
    try:
        return loads_strict(text)
    except json.JSONDecodeError as exc:
        issue = RecordIssue("invalid_json", "", f"line is not valid JSON: {exc.msg} (column {exc.colno})")
        return Quarantined(line, (issue,), raw=text)
    except StrictJSONError as exc:
        return Quarantined(line, (RecordIssue(exc.code, "", str(exc)),), raw=text)


def validate_lines(
    lines: Iterable[tuple[int, bytes]],
    *,
    kind: RecordKind,
    config: ValidateStageSpec,
) -> Iterator[Outcome]:
    """Validate ``(line_number, content)`` pairs, yielding one outcome per non-blank line, in order.

    Pure apart from its own duplicate-id bookkeeping: no files, no logging.
    Ids seen so far are kept in memory (one entry per valid record).
    """
    first_line_of: dict[str, int] = {}
    for number, content in lines:
        if not content.strip():
            continue
        decoded = _decode(number, content)
        if isinstance(decoded, Quarantined):
            yield decoded
            continue
        try:
            record = parse_record(decoded, kind, unknown_fields=config.unknown_fields)
        except RecordValidationError as exc:
            yield Quarantined(number, exc.issues, record=decoded)
            continue

        explicit_id = record.id is not None
        record = record.with_record_id()
        record_id = record.record_id
        if config.reject_duplicate_ids and record_id in first_line_of:
            detail = "was already used by" if explicit_id else "matches the content of"
            issue = RecordIssue(
                "duplicate_id",
                "id" if explicit_id else "",
                f"id {record_id!r} {detail} line {first_line_of[record_id]}",
            )
            yield Quarantined(number, (issue,), record=decoded)
            continue
        try:
            data = encode_json_line(record.to_json_dict())
        except UnicodeEncodeError:
            issue = RecordIssue(
                "invalid_unicode",
                "",
                "contains a lone UTF-16 surrogate (\\ud800-\\udfff) that cannot be written as UTF-8",
            )
            yield Quarantined(number, (issue,), record=decoded)
            continue
        first_line_of.setdefault(record_id, number)
        yield Accepted(number, record_id, data)


@dataclass(frozen=True, slots=True)
class ValidateReport:
    """What one run of the stage did."""

    input_path: Path
    output_path: Path
    quarantine_path: Path
    total: int
    valid: int
    reasons: Mapping[str, int] = field(default_factory=dict)
    """Number of quarantined records per reason code (a record counts once per distinct code)."""

    @property
    def invalid(self) -> int:
        return self.total - self.valid

    @property
    def invalid_fraction(self) -> float:
        return self.invalid / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": STAGE,
            "input": str(self.input_path),
            "output": str(self.output_path),
            "quarantine": str(self.quarantine_path),
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "invalid_fraction": round(self.invalid_fraction, 6),
            "reasons": dict(self.reasons),
        }


def _check_paths(input_path: Path, output_path: Path, quarantine_path: Path) -> None:
    source = input_path.resolve()
    for target in (output_path, quarantine_path):
        if target.resolve() == source:
            raise ConfigError(f"{STAGE}: output {target} would overwrite the input file", details={"path": str(target)})


def run_validate(
    input_path: Path,
    output_dir: Path,
    *,
    kind: RecordKind,
    config: ValidateStageSpec | None = None,
) -> ValidateReport:
    """Validate ``input_path`` and write the valid and quarantine files into ``output_dir``.

    Raises :class:`~curator.errors.InputFileError` if the input cannot be read,
    :class:`~curator.errors.ConfigError` if an output would overwrite the
    input, and :class:`~curator.errors.QuarantineThresholdError` when too many
    records are rejected.
    """
    config = config if config is not None else ValidateStageSpec()
    output_path = output_dir / config.output_file
    quarantine_path = output_dir / config.quarantine_file
    _check_paths(input_path, output_path, quarantine_path)

    with log_context(stage=STAGE), closing(read_lines(input_path)) as lines:
        output_dir.mkdir(parents=True, exist_ok=True)
        total = valid = 0
        reasons: Counter[str] = Counter()
        with AtomicFile(output_path) as valid_file, AtomicFile(quarantine_path) as quarantine_file:
            for outcome in validate_lines(lines, kind=kind, config=config):
                total += 1
                if isinstance(outcome, Accepted):
                    valid += 1
                    valid_file.write(outcome.data)
                    continue
                codes = sorted({issue.code for issue in outcome.issues})
                reasons.update(codes)
                quarantine_file.write(encode_json_line_lossless(outcome.to_dict()))
                logger.debug("validate.record_quarantined", extra={"line": outcome.line, "codes": codes})

            report = ValidateReport(
                input_path=input_path,
                output_path=output_path,
                quarantine_path=quarantine_path,
                total=total,
                valid=valid,
                reasons=dict(sorted(reasons.items())),
            )
            quarantine_file.commit()
            if total == 0:
                logger.warning("validate.empty_input", extra={"input": input_path})
            logger.info("validate.finished", extra=report.to_dict())

            limit = config.max_invalid_fraction
            if limit is not None and report.invalid_fraction > limit:
                # valid_file is discarded on the way out; also drop an earlier run's output so it is not mistaken
                # for this run's result.
                output_path.unlink(missing_ok=True)
                raise QuarantineThresholdError(
                    stage=STAGE,
                    invalid=report.invalid,
                    total=total,
                    max_fraction=limit,
                    quarantine_path=quarantine_path,
                )
            valid_file.commit()
    return report


__all__ = ["STAGE", "Accepted", "Outcome", "Quarantined", "ValidateReport", "run_validate", "validate_lines"]

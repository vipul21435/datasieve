"""The ``ledger`` stage: remember what earlier runs delivered and skip it the next time round.

The input is one valid record per line (the ``validate`` or ``dedup`` output).
Each record's text fields are normalised, joined and hashed exactly as the
``dedup`` stage does, and the digest is looked up in the SQLite ledger
(:class:`curator.ledger.Ledger`):

* **new**: no run has seen the content. It is recorded under this run's id
  and kept;
* **rerun**: an earlier run saw it while reading the same input bytes (or it
  appeared earlier in this run). It is kept, so re-running a spec gives
  byte-identical output, and reported in ``seen_file`` with ``"action":
  "kept"``;
* **seen**: an earlier run over *different* input delivered it. It is skipped
  and reported in ``seen_file`` with ``"action": "skipped"``.

Every ``seen_file`` entry names the earlier run, the id the content had
there and that run's input, so a skipped record can be traced::

    {"line": 4, "id": "sft-104", "digest": "...", "action": "skipped", "seen_run": "batch-1-2026...",
     "seen_id": "sft-004", "seen_source": "data/batch-1.jsonl"}

Duplicates inside one input are the ``dedup`` stage's job and pass through
(counted as ``rerun``). Nothing is committed to the ledger until the stage
has written its files, so a failed run leaves the ledger untouched.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Final

from curator.config.spec import LedgerStageSpec
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
from curator.ledger import DEFAULT_LEDGER_NAME
from curator.ledger import Ledger
from curator.ledger import RunInfo
from curator.ledger import new_run
from curator.log import log_context
from curator.schemas.fields import record_texts
from curator.schemas.parse import Record
from curator.schemas.parse import RecordKind
from curator.schemas.parse import parse_record

STAGE: Final = "ledger"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LedgerReport:
    """What one run of the stage did."""

    input_path: Path
    output_path: Path
    seen_path: Path
    ledger_path: Path
    run_id: str
    total: int
    kept: int
    new: int
    """Records whose content no run had seen (recorded under ``run_id``)."""
    rerun: int
    """Records seen before over the same input bytes; kept."""
    earlier_runs: Mapping[str, int] = field(default_factory=dict)
    """How many seen records each earlier run accounts for."""

    @property
    def skipped(self) -> int:
        return self.total - self.kept

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": STAGE,
            "input": str(self.input_path),
            "output": str(self.output_path),
            "seen": str(self.seen_path),
            "ledger": str(self.ledger_path),
            "run_id": self.run_id,
            "total": self.total,
            "kept": self.kept,
            "dropped": self.skipped,
            "dropped_fraction": round(self.skipped / self.total, 6) if self.total else 0.0,
            "reasons": {"seen": self.skipped} if self.skipped else {},
            "new": self.new,
            "rerun": self.rerun,
            "earlier_runs": dict(self.earlier_runs),
        }


def _decode_record(content: bytes, *, kind: RecordKind, line: int, path: Path) -> Record:
    """Decode one input line into a typed record, or explain why it is not one."""
    where = f"line {line} of {path}"
    try:
        decoded = loads_strict(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, StrictJSONError) as exc:
        raise StageInputError(f"{STAGE}: {where} is not valid JSON: {exc}", stage=STAGE, line=line, path=path) from None
    if not isinstance(decoded, dict):
        raise StageInputError(f"{STAGE}: {where} is not a JSON object", stage=STAGE, line=line, path=path)
    try:
        record = parse_record(decoded, kind)
    except RecordValidationError as exc:
        raise StageInputError(
            f"{STAGE}: {where} is not a valid {kind} record ({exc}); run the validate stage first",
            stage=STAGE,
            line=line,
            path=path,
        ) from None
    return record


def ledger_path_for(config: LedgerStageSpec, *, work_dir: Path) -> Path:
    """The ledger file a stage uses: its ``path``, else ``<work_dir>/ledger.sqlite``."""
    return config.path if config.path is not None else work_dir / DEFAULT_LEDGER_NAME


def run_ledger(
    input_path: Path,
    output_dir: Path,
    *,
    kind: RecordKind,
    config: LedgerStageSpec | None = None,
    run: RunInfo | None = None,
    work_dir: Path | None = None,
) -> LedgerReport:
    """Look every record of ``input_path`` up in the ledger; write the kept ones and the seen report.

    ``run`` identifies the pipeline run (see :func:`curator.ledger.new_run`);
    without one the stage describes an ad-hoc run over ``input_path`` with
    its own config. ``work_dir`` anchors the default ledger path and falls
    back to ``output_dir``. Raises :class:`~curator.errors.InputFileError`
    if the input cannot be read, :class:`~curator.errors.StageInputError`
    for a line that is not a valid record, :class:`~curator.errors.LedgerError`
    if the ledger file is not a SQLite database, and
    :class:`~curator.errors.ConfigError` if an output would overwrite the input.
    """
    config = config if config is not None else LedgerStageSpec()
    output_path = output_dir / config.output_file
    seen_path = output_dir / config.seen_file
    ledger_path = ledger_path_for(config, work_dir=work_dir if work_dir is not None else output_dir)
    for target in (output_path, seen_path):
        if target.resolve() == input_path.resolve():
            raise ConfigError(f"{STAGE}: output {target} would overwrite the input file", details={"path": str(target)})
    run = run if run is not None else new_run("adhoc", input_path, config.model_dump(mode="json"))
    fields = config.fields_for(kind)
    normalizer = TextNormalizer(
        lowercase=config.normalize.lowercase,
        collapse_whitespace=config.normalize.collapse_whitespace,
        strip_punctuation=config.normalize.strip_punctuation,
    )

    with log_context(stage=STAGE, run_id=run.run_id), closing(read_lines(input_path)) as lines:
        output_dir.mkdir(parents=True, exist_ok=True)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        total = kept = new = rerun = 0
        earlier: Counter[str] = Counter()
        with Ledger(ledger_path) as ledger, AtomicFile(output_path) as output_file, AtomicFile(seen_path) as seen_file:
            ledger.begin_run(run)
            for number, content in lines:
                if not content.strip():
                    continue
                total += 1
                record = _decode_record(content, kind=kind, line=number, path=input_path)
                record_id = record.record_id
                digest = content_hash(normalizer.join(record_texts(record), fields), config.hash)
                sighting = ledger.lookup(digest)
                ledger.record(digest, record_id, run.run_id, number)
                if sighting is None:
                    new += 1
                    kept += 1
                    output_file.write(content + b"\n")
                    continue
                same_input = sighting.run_id == run.run_id or sighting.source_digest == run.source_digest
                if same_input:
                    rerun += 1
                    kept += 1
                    output_file.write(content + b"\n")
                earlier[sighting.run_id] += 1
                entry = {
                    "line": number,
                    "id": record_id,
                    "digest": digest,
                    "action": "kept" if same_input else "skipped",
                    "seen_run": sighting.run_id,
                    "seen_id": sighting.record_id,
                    "seen_source": sighting.source,
                }
                seen_file.write(encode_json_line_lossless(entry))
                logger.debug("ledger.record_seen", extra=entry)
            report = LedgerReport(
                input_path=input_path,
                output_path=output_path,
                seen_path=seen_path,
                ledger_path=ledger_path,
                run_id=run.run_id,
                total=total,
                kept=kept,
                new=new,
                rerun=rerun,
                earlier_runs=dict(sorted(earlier.items())),
            )
            ledger.finish_run(run.run_id, total=total, kept=kept, skipped=report.skipped, rerun=rerun)
            output_file.commit()
            seen_file.commit()
            ledger.commit()
        if total == 0:
            logger.warning("ledger.empty_input", extra={"input": input_path})
        logger.info("ledger.finished", extra=report.to_dict())
    return report


__all__ = ["STAGE", "LedgerReport", "ledger_path_for", "run_ledger"]

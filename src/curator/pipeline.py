"""Run a pipeline spec end to end: each stage reads the previous stage's output.

The first stage (``validate``) reads ``input.path``; every later stage reads
the file the stage before it wrote, and all files land in the run's output
directory (``output.dir``, or ``<work_dir>/<name>``). The result collects
each stage's report so one summary describes the whole run::

    {"pipeline": "sft-demo", "input": "...", "output_dir": "...", "output": ".../deduped.jsonl",
     "stages": [{"stage": "validate", "total": 10, "valid": 9, ...}, {"stage": "dedup", "kept": 3, ...}]}

A stage that fails raises its own :class:`~curator.errors.CuratorError`; the
stages before it have already written their files, and the later ones do not
run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from typing import assert_never

from curator.config.spec import DedupStageSpec
from curator.config.spec import LedgerStageSpec
from curator.config.spec import PipelineSpec
from curator.config.spec import StageSpec
from curator.config.spec import ValidateStageSpec
from curator.ledger import RunInfo
from curator.ledger import new_run
from curator.log import log_context
from curator.schemas.parse import RecordKind
from curator.stages.dedup import run_dedup
from curator.stages.ledger import run_ledger
from curator.stages.validate import run_validate

logger = logging.getLogger(__name__)


class StageReport(Protocol):
    """What every stage's report offers the runner: the file it produced and a JSON-safe summary."""

    @property
    def output_path(self) -> Path: ...

    def to_dict(self) -> dict[str, object]: ...


def run_stage(
    stage: StageSpec,
    input_path: Path,
    output_dir: Path,
    *,
    kind: RecordKind,
    run: RunInfo | None = None,
    work_dir: Path | None = None,
) -> StageReport:
    """Run one stage on ``input_path``, writing its files into ``output_dir``.

    ``run`` and ``work_dir`` only matter to the ``ledger`` stage: the run it
    records, and where its default ledger file lives.
    """
    if isinstance(stage, ValidateStageSpec):
        return run_validate(input_path, output_dir, kind=kind, config=stage)
    if isinstance(stage, DedupStageSpec):
        return run_dedup(input_path, output_dir, kind=kind, config=stage)
    if isinstance(stage, LedgerStageSpec):
        return run_ledger(input_path, output_dir, kind=kind, config=stage, run=run, work_dir=work_dir)
    assert_never(stage)  # pragma: no cover - the StageSpec union is exhausted above


@dataclass(frozen=True, slots=True)
class PipelineReport:
    """What one run did: the spec's name, where it read and wrote, and every stage's report in order."""

    name: str
    input_path: Path
    output_dir: Path
    stages: tuple[StageReport, ...]

    @property
    def output_path(self) -> Path:
        """The last stage's output file: the curated dataset."""
        return self.stages[-1].output_path

    def to_dict(self) -> dict[str, object]:
        return {
            "pipeline": self.name,
            "input": str(self.input_path),
            "output_dir": str(self.output_dir),
            "output": str(self.output_path),
            "stages": [stage.to_dict() for stage in self.stages],
        }


def run_pipeline(spec: PipelineSpec, *, work_dir: Path) -> PipelineReport:
    """Run every stage of ``spec`` in order and return the combined report.

    ``work_dir`` (the ``CURATOR_WORK_DIR`` setting) only matters when the spec
    sets no ``output.dir``.
    """
    output_dir = spec.output_dir(work_dir)
    run = None
    if any(isinstance(stage, LedgerStageSpec) for stage in spec.stages):
        run = new_run(spec.name, spec.input.path, spec.model_dump(mode="json"))
    with log_context(pipeline=spec.name):
        logger.info(
            "pipeline.started",
            extra={
                "input": spec.input.path,
                "kind": spec.input.kind,
                "output_dir": output_dir,
                "stages": [stage.stage for stage in spec.stages],
                "run_id": run.run_id if run is not None else None,
            },
        )
        reports: list[StageReport] = []
        current = spec.input.path
        for stage in spec.stages:
            report = run_stage(stage, current, output_dir, kind=spec.input.kind, run=run, work_dir=work_dir)
            reports.append(report)
            current = report.output_path
        result = PipelineReport(spec.name, spec.input.path, output_dir, tuple(reports))
        logger.info("pipeline.finished", extra=result.to_dict())
    return result


__all__ = ["PipelineReport", "StageReport", "run_pipeline", "run_stage"]

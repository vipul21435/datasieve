"""The ``curator`` command line: ``python -m curator run SPEC`` and ``python -m curator check SPEC``.

``run`` executes a pipeline spec and prints the stage funnel::

    pipeline sft-demo-300: examples/data/sft_demo.jsonl -> .curator/sft-demo-300/deduped.jsonl
      validate     299 in ->   287 out   (12 quarantined: blank_text=3, invalid_json=2, ...)
      dedup        287 in ->   220 out   (67 dropped: exact_duplicate=30, near_duplicate=25, contaminated=12)
    finished in 0.41s (729 records/s)

``--json`` prints the full report (:meth:`curator.pipeline.PipelineReport.to_dict`
plus ``elapsed_seconds`` and ``records_per_second``) instead. ``check`` only
loads the spec and lists its stages, so a typo is caught before a long run.

Exit codes follow :mod:`curator.errors`: 0 on success, 78 for a bad spec or
setting, 65/66 for unusable data, 2 for a usage error (argparse).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO
from typing import get_args

from curator import __version__
from curator.config.settings import load_settings
from curator.config.spec import PipelineSpec
from curator.config.spec import load_spec
from curator.errors import CuratorError
from curator.log import LogFormat
from curator.log import LogLevel
from curator.log import configure_logging
from curator.pipeline import PipelineReport
from curator.pipeline import run_pipeline

PROG = "python -m curator"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Validate and deduplicate SFT and preference datasets from a pipeline spec."
    )
    parser.add_argument("--version", action="version", version=f"curator {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", choices=get_args(LogLevel), help="override CURATOR_LOG_LEVEL")
    common.add_argument("--log-format", choices=get_args(LogFormat), help="override CURATOR_LOG_FORMAT")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser("run", parents=[common], help="run every stage of a pipeline spec and print the funnel")
    run.add_argument("spec", type=Path, help="pipeline spec (.yaml, .toml or .json)")
    run.add_argument("--work-dir", type=Path, help="override CURATOR_WORK_DIR (used when the spec sets no output.dir)")
    run.add_argument("--json", action="store_true", help="print the full report as JSON instead of the funnel")

    check = commands.add_parser(
        "check", parents=[common], help="load a pipeline spec and list its stages without running it"
    )
    check.add_argument("spec", type=Path, help="pipeline spec (.yaml, .toml or .json)")
    return parser


def _stage_line(summary: dict[str, object]) -> str:
    """One funnel line: ``validate 299 in -> 287 out (12 quarantined: blank_text=3, ...)``."""
    stage = str(summary["stage"])
    total = int(str(summary["total"]))
    if stage == "validate":
        kept, dropped, verb = int(str(summary["valid"])), int(str(summary["invalid"])), "quarantined"
    else:
        verb = "skipped" if stage == "ledger" else "dropped"
        kept, dropped = int(str(summary["kept"])), int(str(summary["dropped"]))
    reasons = summary.get("reasons")
    detail = (
        ", ".join(f"{code}={count}" for code, count in sorted(reasons.items())) if isinstance(reasons, dict) else ""
    )
    tail = f"({dropped} {verb}: {detail})" if detail else f"({dropped} {verb})"
    return f"  {stage:<10} {total:>6} in -> {kept:>6} out   {tail}"


def format_funnel(report: PipelineReport, elapsed: float) -> str:
    """The human-readable summary of a run."""
    stages = [stage.to_dict() for stage in report.stages]
    first_total = int(str(stages[0]["total"])) if stages else 0
    rate = f"{first_total / elapsed:,.0f} records/s" if elapsed > 0 else "n/a"
    lines = [
        f"pipeline {report.name}: {report.input_path} -> {report.output_path}",
        *(_stage_line(summary) for summary in stages),
        f"finished in {elapsed:.2f}s ({rate})",
    ]
    return "\n".join(lines)


def report_json(report: PipelineReport, elapsed: float) -> dict[str, object]:
    data = report.to_dict()
    stages = report.stages
    first_total = int(str(stages[0].to_dict()["total"])) if stages else 0
    data["elapsed_seconds"] = round(elapsed, 6)
    data["records_per_second"] = round(first_total / elapsed, 1) if elapsed > 0 else None
    return data


def describe_spec(spec: PipelineSpec) -> str:
    lines = [f"{spec.name}: {spec.input.kind} records from {spec.input.path}"]
    if spec.description:
        lines.append(f"  {spec.description}")
    lines.extend(f"  {index}. {stage.stage}" for index, stage in enumerate(spec.stages, start=1))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Entry point; returns the process exit code instead of calling :func:`sys.exit`."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(argv)
    try:
        overrides = {
            key: value
            for key, value in (
                ("log_level", args.log_level),
                ("log_format", args.log_format),
                ("work_dir", getattr(args, "work_dir", None)),
            )
            if value is not None
        }
        settings = load_settings(**overrides)
        configure_logging(settings.log_level, settings.log_format, stream=err)
        spec = load_spec(args.spec)
        if args.command == "check":
            print(describe_spec(spec), file=out)
            return 0
        started = time.perf_counter()
        report = run_pipeline(spec, work_dir=settings.work_dir)
        elapsed = time.perf_counter() - started
        if args.json:
            print(json.dumps(report_json(report, elapsed), indent=2), file=out)
        else:
            print(format_funnel(report, elapsed), file=out)
    except CuratorError as exc:
        print(f"error [{exc.code}]: {exc}", file=err)
        return exc.exit_code
    return 0


__all__ = ["build_parser", "describe_spec", "format_funnel", "main", "report_json"]

"""The pipeline runner: stages run in order, each on the previous stage's output, with one combined report."""

import json
from pathlib import Path
from typing import Any

import pytest

from curator.config import DedupStageSpec
from curator.config import ValidateStageSpec
from curator.config import parse_spec
from curator.errors import QuarantineThresholdError
from curator.pipeline import PipelineReport
from curator.pipeline import run_pipeline
from curator.pipeline import run_stage
from curator.stages.dedup import DedupReport
from curator.stages.validate import ValidateReport

from .conftest import JsonLogLines

PROMPT = (
    "Give me three practical tips for keeping a small balcony herb garden alive through a hot summer, "
    "covering watering, shade and which herbs cope best with heat."
)
RESPONSE = (
    "Water early in the morning and again at dusk on the hottest days, move the pots into afternoon shade, "
    "and favour heat lovers such as rosemary, thyme and oregano over basil and coriander."
)
RECORDS: list[dict[str, Any]] = [
    {"id": "r-1", "prompt": PROMPT, "response": RESPONSE},
    {"id": "r-2", "prompt": PROMPT.upper(), "response": RESPONSE},  # exact duplicate after normalisation
    {"id": "r-3", "prompt": PROMPT.replace("three", "four"), "response": RESPONSE},  # near duplicate
    {"id": "r-4", "prompt": "What is 2 + 2?", "response": ""},  # quarantined by validate
    {"id": "r-5", "prompt": "What is the boiling point of water at sea level?", "response": "100 degrees Celsius."},
    {"id": "r-6", "prompt": "Name a prime number below ten.", "response": "Seven."},
]
EVAL = [{"id": "e-1", "prompt": "What is the boiling point of water at sea level?", "response": "100 degrees Celsius."}]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def spec_data(tmp_path: Path) -> dict[str, Any]:
    write_jsonl(tmp_path / "raw.jsonl", RECORDS)
    write_jsonl(tmp_path / "eval.jsonl", EVAL)
    return {
        "version": 1,
        "name": "herbs",
        "input": {"path": "raw.jsonl", "kind": "sft"},
        "stages": [{"stage": "validate"}, {"stage": "dedup", "reference": {"path": "eval.jsonl"}}],
    }


def test_stages_chain_and_the_report_summarises_them(spec_data: dict[str, Any], tmp_path: Path) -> None:
    spec = parse_spec(spec_data, base_dir=tmp_path)
    report = run_pipeline(spec, work_dir=tmp_path / "work")

    validate, dedup = report.stages
    assert isinstance(validate, ValidateReport) and isinstance(dedup, DedupReport)
    assert report.output_dir == tmp_path / "work" / "herbs"
    assert (validate.total, validate.valid) == (6, 5)
    assert dedup.input_path == validate.output_path  # the dedup stage reads the validated file
    assert (dedup.total, dedup.kept) == (5, 2)
    assert dedup.reasons == {"contaminated": 1, "exact_duplicate": 1, "near_duplicate": 1}
    assert report.output_path == dedup.output_path == tmp_path / "work" / "herbs" / "deduped.jsonl"
    assert [r["id"] for r in read_jsonl(report.output_path)] == ["r-1", "r-6"]

    summary = report.to_dict()
    assert json.loads(json.dumps(summary)) == summary
    assert (summary["pipeline"], summary["input"], summary["output"]) == (
        "herbs",
        str(tmp_path / "raw.jsonl"),
        str(report.output_path),
    )
    assert [stage["stage"] for stage in summary["stages"]] == ["validate", "dedup"]
    assert summary["stages"][1]["reference_size"] == 1


def test_explicit_output_dir_wins_over_the_work_dir(spec_data: dict[str, Any], tmp_path: Path) -> None:
    spec = parse_spec(spec_data | {"output": {"dir": "runs/here"}}, base_dir=tmp_path)
    report = run_pipeline(spec, work_dir=tmp_path / "ignored")
    assert report.output_dir == tmp_path / "runs" / "here"
    assert sorted(p.name for p in report.output_dir.iterdir()) == [
        "dedup_clusters.jsonl",
        "dedup_rejects.jsonl",
        "deduped.jsonl",
        "quarantine.jsonl",
        "validated.jsonl",
    ]
    assert not (tmp_path / "ignored").exists()


def test_a_failing_stage_stops_the_run_and_keeps_earlier_outputs(spec_data: dict[str, Any], tmp_path: Path) -> None:
    spec_data["stages"][0]["max_invalid_fraction"] = 0.1
    spec = parse_spec(spec_data, base_dir=tmp_path)

    with pytest.raises(QuarantineThresholdError):
        run_pipeline(spec, work_dir=tmp_path / "work")

    out = tmp_path / "work" / "herbs"
    assert sorted(p.name for p in out.iterdir()) == [
        "quarantine.jsonl"
    ]  # validate withheld its output; dedup never ran


def test_run_stage_dispatches_on_the_spec_type(tmp_path: Path) -> None:
    raw = write_jsonl(tmp_path / "raw.jsonl", RECORDS)
    validated = run_stage(ValidateStageSpec(), raw, tmp_path / "out", kind="sft")
    assert isinstance(validated, ValidateReport)
    deduped = run_stage(DedupStageSpec(), validated.output_path, tmp_path / "out", kind="sft")
    assert isinstance(deduped, DedupReport)
    assert deduped.input_path == validated.output_path


def test_logs_bind_the_pipeline_name_and_each_stage(
    spec_data: dict[str, Any], tmp_path: Path, json_log: JsonLogLines
) -> None:
    run_pipeline(parse_spec(spec_data, base_dir=tmp_path), work_dir=tmp_path / "work")

    lines = json_log()
    assert {entry["pipeline"] for entry in lines} == {"herbs"}
    events = [entry["event"] for entry in lines]
    assert events[0] == "pipeline.started" and events[-1] == "pipeline.finished"
    assert {entry.get("stage") for entry in lines if entry["event"].startswith("validate.")} == {"validate"}
    assert {entry.get("stage") for entry in lines if entry["event"].startswith("dedup.")} == {"dedup"}
    assert "stage" not in lines[-1]
    assert [stage["stage"] for stage in lines[-1]["stages"]] == ["validate", "dedup"]


def test_report_output_is_the_last_stage(tmp_path: Path) -> None:
    first = ValidateReport(tmp_path / "a", tmp_path / "b", tmp_path / "q", total=1, valid=1)
    second = DedupReport(tmp_path / "b", tmp_path / "c", tmp_path / "r", tmp_path / "k", total=1, kept=1)
    report = PipelineReport("demo", tmp_path / "a", tmp_path, (first, second))
    assert report.output_path == tmp_path / "c"
    assert report.to_dict()["stages"][1]["kept"] == 1

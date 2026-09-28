"""The example specs under examples/ load and run as documented."""

import json
from pathlib import Path

import pytest

from curator.config import DedupStageSpec
from curator.config import ValidateStageSpec
from curator.config import load_settings
from curator.config import load_spec
from curator.pipeline import run_pipeline
from curator.stages.dedup import DedupReport
from curator.stages.validate import run_validate

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize(
    ("spec_file", "total", "valid", "reasons"),
    [
        (
            "sft-validate.yaml",
            14,
            8,
            {"blank_text": 1, "duplicate_id": 2, "invalid_json": 1, "last_turn_not_assistant": 1, "literal_error": 1},
        ),
        (
            "preference-validate.toml",
            6,
            3,
            {"chosen_scored_below_rejected": 1, "identical_responses": 1, "missing": 1},
        ),
    ],
)
def test_example_runs_as_documented(
    spec_file: str, total: int, valid: int, reasons: dict[str, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # relative paths in the spec must not depend on the cwd
    settings = load_settings(_env_file=None, work_dir=tmp_path / "work")
    spec = load_spec(EXAMPLES / spec_file)
    [stage] = spec.stages
    assert isinstance(stage, ValidateStageSpec)

    report = run_validate(spec.input.path, spec.output_dir(settings.work_dir), kind=spec.input.kind, config=stage)

    assert report.output_path.parent == tmp_path / "work" / spec.name
    assert (report.total, report.valid, report.reasons) == (total, valid, reasons)


def test_dedup_example_runs_as_documented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings(_env_file=None, work_dir=tmp_path / "work")
    spec = load_spec(EXAMPLES / "sft-dedup.yaml")
    assert [type(stage) for stage in spec.stages] == [ValidateStageSpec, DedupStageSpec]

    report = run_pipeline(spec, work_dir=settings.work_dir)

    validate, dedup = report.to_dict()["stages"]
    assert (validate["total"], validate["valid"], validate["reasons"]) == (10, 9, {"blank_text": 1})
    assert (dedup["total"], dedup["kept"], dedup["groups"], dedup["reference_size"]) == (9, 3, 2, 3)
    assert dedup["reasons"] == {"contaminated": 2, "exact_duplicate": 3, "near_duplicate": 1}
    assert report.output_path == tmp_path / "work" / "sft-dedup-demo" / "deduped.jsonl"

    stage = report.stages[1]
    assert isinstance(stage, DedupReport)
    rejects = [json.loads(text) for text in stage.rejects_path.read_text(encoding="utf-8").splitlines()]
    assert [(r["id"], r["reason"], r["match"], r["similarity"]) for r in rejects] == [
        ("sft-102", "exact_duplicate", "sft-101", 1.0),
        ("sft-103", "near_duplicate", "sft-101", 0.928571),
        ("sft-104", "exact_duplicate", "sft-101", 1.0),
        ("sft-107", "contaminated", "eval-201", 1.0),
        ("sft-108", "contaminated", "eval-201", 0.954023),
        ("sft-110", "exact_duplicate", "sft-105", 1.0),
    ]
    kept = [json.loads(text)["id"] for text in report.output_path.read_text(encoding="utf-8").splitlines()]
    assert kept == ["sft-101", "sft-105", "sft-109"]

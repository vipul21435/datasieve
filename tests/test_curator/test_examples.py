"""The example specs under examples/ load and run as documented."""

import json
from pathlib import Path

import pytest

from curator.config import DedupStageSpec
from curator.config import ValidateStageSpec
from curator.config import load_settings
from curator.config import load_spec
from curator.ledger import Ledger
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


def test_demo_example_runs_as_documented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `make demo` dataset: 299 lines, 12 invalid, 220 unique records after dedup."""
    monkeypatch.chdir(tmp_path)
    spec = load_spec(EXAMPLES / "sft-demo.yaml")

    report = run_pipeline(spec, work_dir=tmp_path / "work")

    validate, dedup, ledger = report.to_dict()["stages"]
    assert (validate["total"], validate["valid"], validate["invalid"]) == (299, 287, 12)
    assert (dedup["total"], dedup["kept"], dedup["reference_size"]) == (287, 220, 15)
    assert dedup["reasons"] == {"contaminated": 12, "exact_duplicate": 30, "near_duplicate": 25}
    assert (ledger["total"], ledger["kept"], ledger["new"], ledger["rerun"]) == (220, 220, 220, 0)
    assert ledger["ledger"] == str(tmp_path / "work" / "ledger.sqlite")
    first = report.output_path.read_bytes()

    again = run_pipeline(spec, work_dir=tmp_path / "work")

    assert again.output_path.read_bytes() == first  # a re-run is byte-identical
    ledger = again.to_dict()["stages"][2]
    assert (ledger["kept"], ledger["new"], ledger["rerun"], ledger["dropped"]) == (220, 0, 220, 0)
    assert ledger["earlier_runs"] == {report.to_dict()["stages"][2]["run_id"]: 220}


def test_demo_data_is_reproducible() -> None:
    """examples/make_demo_data.py regenerates the committed files byte for byte."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_demo_data", EXAMPLES / "make_demo_data.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    train, evaluation = module.build()

    assert "".join(line + "\n" for line in train) == module.TRAIN_PATH.read_text(encoding="utf-8")
    assert "".join(line + "\n" for line in evaluation) == module.EVAL_PATH.read_text(encoding="utf-8")
    assert len(train) == 299 and len(evaluation) == 15


def test_ledger_batch_examples_run_as_documented(tmp_path: Path) -> None:
    """Two batches through one ledger: batch 2 skips batch 1's content and both collision kinds appear."""
    work = tmp_path / "work"
    first = run_pipeline(load_spec(EXAMPLES / "ledger-batch-1.yaml"), work_dir=work)
    second = run_pipeline(load_spec(EXAMPLES / "ledger-batch-2.yaml"), work_dir=work)

    ledger_1, ledger_2 = first.to_dict()["stages"][1], second.to_dict()["stages"][1]
    assert (ledger_1["total"], ledger_1["kept"], ledger_1["new"]) == (2, 2, 2)
    assert (ledger_2["total"], ledger_2["kept"], ledger_2["new"], ledger_2["dropped"]) == (3, 2, 2, 1)
    assert ledger_2["reasons"] == {"seen": 1} and ledger_2["earlier_runs"] == {ledger_1["run_id"]: 1}
    seen = json.loads((work / "ledger-batch-2" / "seen.jsonl").read_text())
    assert (seen["line"], seen["id"], seen["action"], seen["seen_id"]) == (2, "c", "skipped", "b")
    assert seen["seen_source"] == str(EXAMPLES / "data" / "ledger_batch_1.jsonl")
    with Ledger.open_existing(work / "ledger.sqlite") as ledger:
        stats = ledger.stats()
        collisions = ledger.collisions()
    assert (stats["runs"], stats["records"], stats["distinct_contents"], stats["distinct_ids"]) == (2, 5, 4, 4)
    assert [(c.kind, c.key) for c in collisions] == [("same_id", "a"), ("same_content", collisions[1].key)]
    assert [[m.record_id for m in c.members] for c in collisions] == [["a", "a"], ["b", "c"]]

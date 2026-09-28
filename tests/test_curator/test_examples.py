"""The example specs under examples/ load and run as documented."""

from pathlib import Path

import pytest

from curator.config import ValidateStageSpec
from curator.config import load_settings
from curator.config import load_spec
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

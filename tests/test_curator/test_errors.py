"""Contract tests for curator's error hierarchy."""

import json
from pathlib import Path

import pytest

from curator.errors import EX_CONFIG
from curator.errors import EX_DATAERR
from curator.errors import EX_FAILURE
from curator.errors import EX_NOINPUT
from curator.errors import ConfigError
from curator.errors import CuratorError
from curator.errors import DataError
from curator.errors import InputFileError
from curator.errors import QuarantineThresholdError
from curator.errors import RecordValidationError
from curator.errors import SettingsError
from curator.errors import SpecError
from curator.schemas.issues import RecordIssue

ALL_ERRORS: list[CuratorError] = [
    CuratorError("generic"),
    ConfigError("bad config"),
    SettingsError("bad settings", problems=["CURATOR_LOG_LEVEL: nope"]),
    SpecError("bad spec", path=Path("spec.yaml"), problems=["name: missing"]),
    DataError("bad data"),
    InputFileError("missing input", path=Path("in.jsonl")),
    RecordValidationError([RecordIssue("missing", "prompt", "Field required")]),
    QuarantineThresholdError(stage="validate", invalid=3, total=4, max_fraction=0.5, quarantine_path=Path("q.jsonl")),
]


@pytest.mark.parametrize("error", ALL_ERRORS, ids=lambda e: type(e).__name__)
def test_every_error_is_a_curator_error_with_json_safe_dict(error: CuratorError) -> None:
    assert isinstance(error, CuratorError)
    payload = error.to_dict()
    assert payload["error"] == error.code
    assert payload["message"] == error.message
    # The API and the JSON logs serialise this directly.
    assert json.loads(json.dumps(payload)) == payload


def test_codes_are_unique_per_class() -> None:
    codes = [type(error).code for error in ALL_ERRORS]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize(
    ("error_type", "exit_code"),
    [
        (CuratorError, EX_FAILURE),
        (ConfigError, EX_CONFIG),
        (SettingsError, EX_CONFIG),
        (SpecError, EX_CONFIG),
        (DataError, EX_DATAERR),
        (InputFileError, EX_NOINPUT),
        (RecordValidationError, EX_DATAERR),
        (QuarantineThresholdError, EX_DATAERR),
    ],
)
def test_exit_codes_follow_sysexits(error_type: type[CuratorError], exit_code: int) -> None:
    assert error_type.exit_code == exit_code


def test_hierarchy_lets_callers_catch_by_failure_class() -> None:
    assert issubclass(SettingsError, ConfigError)
    assert issubclass(SpecError, ConfigError)
    assert issubclass(InputFileError, DataError)
    assert issubclass(RecordValidationError, DataError)
    assert issubclass(QuarantineThresholdError, DataError)
    assert not issubclass(DataError, ConfigError)


def test_spec_error_lists_problems_in_str_and_details() -> None:
    error = SpecError("pipeline spec is invalid", path=Path("p.yaml"), problems=["name: Field required", "x: y"])
    assert str(error) == "pipeline spec is invalid\n  name: Field required\n  x: y"
    assert error.details == {"problems": ["name: Field required", "x: y"], "path": "p.yaml"}


def test_record_validation_error_summarises_all_issues() -> None:
    issues = [
        RecordIssue("missing", "prompt", "Field required"),
        RecordIssue("identical_responses", "", "chosen and rejected are identical"),
    ]
    error = RecordValidationError(issues)
    assert error.issues == tuple(issues)
    assert str(error) == (
        "prompt: Field required [missing]; <record>: chosen and rejected are identical [identical_responses]"
    )
    assert error.details["issues"] == [issue.to_dict() for issue in issues]


def test_quarantine_threshold_error_reports_fraction() -> None:
    error = QuarantineThresholdError(
        stage="validate", invalid=3, total=4, max_fraction=0.5, quarantine_path=Path("out/q.jsonl")
    )
    assert "3/4 records (75.0%)" in str(error)
    assert "limit of 50.0%" in str(error)
    assert error.details["invalid_fraction"] == 0.75

"""Pipeline spec: YAML/TOML/JSON loading, path resolution and error reporting."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from curator.config import PipelineSpec
from curator.config import ValidateStageSpec
from curator.config import load_spec
from curator.config import parse_spec
from curator.errors import SpecError

YAML_SPEC = """\
version: 1
name: support-sft
description: Customer-support SFT data
input:
  path: data/raw.jsonl
  kind: sft
output:
  dir: out
stages:
  - stage: validate
    unknown_fields: metadata
    max_invalid_fraction: 0.25
"""

TOML_SPEC = """\
version = 1
name = "support-sft"
description = "Customer-support SFT data"

[input]
path = "data/raw.jsonl"
kind = "sft"

[output]
dir = "out"

[[stages]]
stage = "validate"
unknown_fields = "metadata"
max_invalid_fraction = 0.25
"""

JSON_SPEC = json.dumps({
    "version": 1,
    "name": "support-sft",
    "description": "Customer-support SFT data",
    "input": {"path": "data/raw.jsonl", "kind": "sft"},
    "output": {"dir": "out"},
    "stages": [{"stage": "validate", "unknown_fields": "metadata", "max_invalid_fraction": 0.25}],
})

MINIMAL: dict[str, Any] = {
    "version": 1,
    "name": "demo",
    "input": {"path": "in.jsonl", "kind": "preference"},
    "stages": [{"stage": "validate"}],
}


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def spec_problems(data: dict[str, Any]) -> list[str]:
    with pytest.raises(SpecError) as excinfo:
        parse_spec(data)
    return list(excinfo.value.problems)


@pytest.mark.parametrize(
    ("name", "text"), [("p.yaml", YAML_SPEC), ("p.yml", YAML_SPEC), ("p.toml", TOML_SPEC), ("p.json", JSON_SPEC)]
)
def test_all_formats_load_to_the_same_spec(tmp_path: Path, name: str, text: str) -> None:
    spec = load_spec(write(tmp_path / "specs", name, text))

    assert spec.name == "support-sft"
    assert spec.input.kind == "sft"
    assert spec.input.path == tmp_path / "specs" / "data" / "raw.jsonl"
    assert spec.output.dir == tmp_path / "specs" / "out"
    assert spec.stages == [ValidateStageSpec(unknown_fields="metadata", max_invalid_fraction=0.25)]


def test_relative_paths_resolve_against_the_spec_file_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path / "project" / "specs", "p.yaml", YAML_SPEC)
    monkeypatch.chdir(tmp_path)

    spec = load_spec(Path("project/specs/p.yaml"))

    assert spec.input.path.resolve() == (path.parent / "data" / "raw.jsonl").resolve()


def test_absolute_paths_are_kept(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "raw.jsonl"
    spec = load_spec(write(tmp_path, "p.yaml", YAML_SPEC.replace("data/raw.jsonl", str(absolute))))
    assert spec.input.path == absolute


def test_parse_spec_without_a_base_dir_keeps_paths_as_given() -> None:
    assert parse_spec(MINIMAL).input.path == Path("in.jsonl")


def test_defaults_of_a_minimal_spec() -> None:
    spec = parse_spec(MINIMAL)
    [stage] = spec.stages

    assert spec.description == ""
    assert spec.output.dir is None
    assert spec.input.format == "jsonl"
    assert stage == ValidateStageSpec()
    assert (stage.unknown_fields, stage.reject_duplicate_ids, stage.max_invalid_fraction) == ("reject", True, None)
    assert (stage.output_file, stage.quarantine_file) == ("validated.jsonl", "quarantine.jsonl")


def test_output_dir_defaults_to_work_dir_and_pipeline_name(tmp_path: Path) -> None:
    assert parse_spec(MINIMAL).output_dir(tmp_path) == tmp_path / "demo"
    explicit = parse_spec(MINIMAL | {"output": {"dir": "/runs/x"}})
    assert explicit.output_dir(tmp_path) == Path("/runs/x")


def test_specs_are_immutable() -> None:
    spec = parse_spec(MINIMAL)
    with pytest.raises(ValueError, match="frozen"):
        spec.name = "other"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"version": None}, "version: Field required [missing]"),
        ({"version": 2}, "version: Input should be 1 [literal_error]"),
        ({"name": "Support SFT"}, "[string_pattern_mismatch]"),
        ({"runner": "local"}, "runner: Extra inputs are not permitted [extra_forbidden]"),
        ({"input": {"path": "x.jsonl", "kind": "pretrain"}}, "input.kind: Input should be 'sft' or 'preference'"),
        ({"input": {"path": "x.jsonl", "kind": "sft", "format": "csv"}}, "input.format: Input should be 'jsonl'"),
        ({"stages": []}, "stages: List should have at least 1 item"),
        ({"stages": [{"stage": "dedupe"}]}, "stages[0]: Input tag 'dedupe' found using 'stage'"),
        (
            {"stages": [{"stage": "validate", "max_invalid_fracton": 0.1}]},
            "stages[0].max_invalid_fracton: Extra inputs",
        ),
        ({"stages": [{"stage": "validate", "max_invalid_fraction": 1.5}]}, "[less_than_equal]"),
        ({"stages": [{"stage": "validate", "unknown_fields": "keep"}]}, "stages[0].unknown_fields: Input should be"),
        (
            {"stages": [{"stage": "validate", "output_file": "../escape.jsonl"}]},
            "stages[0].output_file: must be a plain",
        ),
        (
            {"stages": [{"stage": "validate", "quarantine_file": "validated.jsonl"}]},
            "stages[0]: output_file and quarantine_file must be different files [same_file]",
        ),
        ({"stages": [{"stage": "validate"}, {"stage": "validate"}]}, "repeated: validate [duplicate_stage]"),
    ],
)
def test_invalid_specs_name_the_offending_field(change: dict[str, Any], expected: str) -> None:
    data = {key: value for key, value in (MINIMAL | change).items() if value is not None}
    problems = spec_problems(data)
    assert any(expected in problem for problem in problems), problems


def test_all_problems_are_reported_at_once() -> None:
    problems = spec_problems({"version": 1, "name": "BAD", "stages": [{"stage": "nope"}]})
    fields = sorted(problem.split(":", 1)[0] for problem in problems)
    assert fields == ["input", "name", "stages[0]"]


def test_spec_error_carries_path_and_problems(tmp_path: Path) -> None:
    path = write(tmp_path, "p.yaml", YAML_SPEC.replace("kind: sft", "kind: chat"))

    with pytest.raises(SpecError) as excinfo:
        load_spec(path)

    error = excinfo.value
    assert error.path == path
    assert error.details["path"] == str(path)
    assert str(error).startswith(f"invalid pipeline spec in {path}\n  input.kind:")
    assert error.exit_code == 78


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("p.yaml", "name: [unclosed\n", "cannot parse pipeline spec"),
        ("p.yaml", "version: 1\nname: a\nname: b\n", "found duplicate key 'name'"),
        ("p.json", '{"version": 1, "version": 1}', "duplicate key 'version'"),
        ("p.json", "{not json", "cannot parse pipeline spec"),
        ("p.toml", "version = 1\nversion = 2\n", "cannot parse pipeline spec"),
        ("p.yaml", "- just\n- a list\n", "the top level must be a mapping"),
        ("p.yaml", "", "the top level must be a mapping"),
        ("p.yaml", "version: !!python/object/apply:os.system ['echo pwned']\n", "cannot parse pipeline spec"),
        ("p.ini", "[x]\n", "unsupported pipeline spec format '.ini'"),
        ("p", "version: 1\n", "unsupported pipeline spec format '(none)'"),
    ],
)
def test_unreadable_spec_files(tmp_path: Path, name: str, text: str, message: str) -> None:
    with pytest.raises(SpecError, match=re.escape(message)):
        load_spec(write(tmp_path, name, text))


def test_missing_spec_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="pipeline spec not found"):
        load_spec(tmp_path / "absent.yaml")


def test_non_utf8_spec_file(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_bytes(b"name: caf\xe9\n")
    with pytest.raises(SpecError, match="cannot read pipeline spec"):
        load_spec(path)


def test_model_can_be_built_in_code() -> None:
    spec = PipelineSpec.model_validate(MINIMAL)
    assert spec == parse_spec(MINIMAL)

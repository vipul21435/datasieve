"""Pipeline spec: YAML/TOML/JSON loading, path resolution and error reporting."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from curator.config import DedupStageSpec
from curator.config import NearDuplicateSpec
from curator.config import NormalizeSpec
from curator.config import PipelineSpec
from curator.config import ReferenceSpec
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


# ---------------------------------------------------------------------------
# dedup stage

DEDUP_YAML = """\
version: 1
name: dedup-demo
input:
  path: data/train.jsonl
  kind: sft
stages:
  - stage: validate
  - stage: dedup
    fields: [prompt]
    normalize:
      strip_punctuation: true
    hash: sha256
    near:
      num_perm: 64
      ngram_size: 2
      threshold: 0.6
      seed: 7
    reference:
      path: data/eval.jsonl
      threshold: 0.5
    output_file: kept.jsonl
    rejects_file: dropped.jsonl
    clusters_file: groups.jsonl
"""


def with_dedup(stage: dict[str, Any], kind: str = "sft") -> dict[str, Any]:
    return MINIMAL | {
        "input": {"path": "in.jsonl", "kind": kind},
        "stages": [{"stage": "validate"}, stage | {"stage": "dedup"}],
    }


def test_dedup_stage_defaults() -> None:
    _, stage = parse_spec(with_dedup({})).stages

    assert stage == DedupStageSpec()
    assert isinstance(stage, DedupStageSpec)
    assert stage.fields is None
    assert stage.normalize == NormalizeSpec(lowercase=True, collapse_whitespace=True, strip_punctuation=False)
    assert stage.hash == "xxhash"
    assert stage.near == NearDuplicateSpec(enabled=True, num_perm=128, ngram_size=3, threshold=0.8, seed=42)
    assert stage.reference is None
    assert (stage.output_file, stage.rejects_file, stage.clusters_file) == (
        "deduped.jsonl",
        "dedup_rejects.jsonl",
        "dedup_clusters.jsonl",
    )


def test_dedup_fields_default_to_every_text_field_of_the_kind() -> None:
    stage = DedupStageSpec()
    assert stage.fields_for("sft") == ("prompt", "response")
    assert stage.fields_for("preference") == ("prompt", "chosen", "rejected")
    assert stage.reference_fields_for("preference") == ("prompt", "chosen", "rejected")

    explicit = DedupStageSpec(fields=["response", "prompt"], reference=ReferenceSpec(path=Path("e.jsonl")))
    assert explicit.fields_for("sft") == ("response", "prompt")  # order as listed
    assert explicit.reference_fields_for("sft") == ("response", "prompt")  # falls back to fields

    narrowed = DedupStageSpec(reference=ReferenceSpec(path=Path("e.jsonl"), fields=["prompt"]))
    assert narrowed.reference_fields_for("sft") == ("prompt",)
    assert narrowed.fields_for("sft") == ("prompt", "response")


def test_dedup_stage_from_yaml_with_every_option(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path / "specs", "p.yaml", DEDUP_YAML))
    _, stage = spec.stages

    assert isinstance(stage, DedupStageSpec)
    assert stage.fields == ["prompt"]
    assert stage.normalize.strip_punctuation is True
    assert stage.hash == "sha256"
    assert stage.near == NearDuplicateSpec(num_perm=64, ngram_size=2, threshold=0.6, seed=7)
    assert stage.reference is not None
    assert stage.reference.path == tmp_path / "specs" / "data" / "eval.jsonl"  # resolved like input.path
    assert (stage.reference.fields, stage.reference.threshold) == (None, 0.5)
    assert (stage.output_file, stage.rejects_file, stage.clusters_file) == (
        "kept.jsonl",
        "dropped.jsonl",
        "groups.jsonl",
    )


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ({"near": {"threshold": 0}}, "stages[1].near.threshold: Input should be greater than 0 [greater_than]"),
        ({"near": {"threshold": 1.5}}, "stages[1].near.threshold: Input should be less than or equal to 1"),
        ({"near": {"num_perm": 8}}, "stages[1].near.num_perm: Input should be greater than or equal to 16"),
        ({"near": {"num_perm": 4096}}, "stages[1].near.num_perm: Input should be less than or equal to 1024"),
        ({"near": {"ngram_size": 0}}, "stages[1].near.ngram_size: Input should be greater than or equal to 1"),
        ({"near": {"seed": "x"}}, "stages[1].near.seed: Input should be a valid integer"),
        ({"near": {"threshhold": 0.9}}, "stages[1].near.threshhold: Extra inputs are not permitted"),
        ({"reference": {"path": "e.jsonl", "threshold": 0}}, "stages[1].reference.threshold: Input should be greater"),
        ({"reference": {"path": "e.jsonl", "threshold": 2}}, "stages[1].reference.threshold: Input should be less"),
        ({"reference": {}}, "stages[1].reference.path: Field required [missing]"),
        ({"reference": {"path": "e.jsonl", "fields": ["prompt", "prompt"]}}, "[duplicate_field]"),
        ({"fields": ["prompt", "prompt"]}, "stages[1].fields: each field may be listed once, got ['prompt', 'prompt']"),
        ({"fields": []}, "stages[1].fields: List should have at least 1 item"),
        ({"fields": ["text"]}, "stages[1].fields[0]: Input should be 'prompt', 'response', 'chosen' or 'rejected'"),
        (
            {"fields": ["chosen"]},
            "fields lists chosen, but sft records only have prompt, response [unknown_text_field]",
        ),
        (
            {"reference": {"path": "e.jsonl", "fields": ["rejected"]}},
            "reference.fields lists rejected, but sft records",
        ),
        ({"hash": "md5"}, "stages[1].hash: Input should be 'xxhash' or 'sha256'"),
        ({"normalize": {"lowercase": "maybe"}}, "stages[1].normalize.lowercase: Input should be a valid boolean"),
        ({"normalize": {"casefold": True}}, "stages[1].normalize.casefold: Extra inputs are not permitted"),
        ({"output_file": "sub/deduped.jsonl"}, "stages[1].output_file: must be a plain file name"),
        (
            {"rejects_file": "deduped.jsonl"},
            "stages[1]: output_file, rejects_file and clusters_file must be different files [same_file]",
        ),
        ({"clusters_file": "dedup_rejects.jsonl"}, "must be different files [same_file]"),
        (
            {"output_file": "validated.jsonl"},
            "stages[1].output_file and stages[0].output_file both write 'validated.jsonl'; every stage output needs its own file [same_file]",
        ),
        ({"clusters_file": "quarantine.jsonl"}, "stages[1].clusters_file and stages[0].quarantine_file both write"),
    ],
)
def test_invalid_dedup_stages_name_the_offending_field(stage: dict[str, Any], expected: str) -> None:
    problems = spec_problems(with_dedup(stage))
    assert any(expected in problem for problem in problems), problems


def test_preference_kind_allows_its_own_text_fields() -> None:
    stage = {"fields": ["chosen", "rejected"], "reference": {"path": "e.jsonl", "fields": ["prompt"]}}
    _, parsed = parse_spec(with_dedup(stage, kind="preference")).stages
    assert isinstance(parsed, DedupStageSpec)
    assert parsed.fields_for("preference") == ("chosen", "rejected")
    assert parsed.reference_fields_for("preference") == ("prompt",)


def test_dedup_cannot_come_before_validate() -> None:
    problems = spec_problems(MINIMAL | {"stages": [{"stage": "dedup"}, {"stage": "validate"}]})
    assert any("the first stage must be 'validate'" in problem for problem in problems), problems


def test_dedup_stage_is_frozen() -> None:
    _, stage = parse_spec(with_dedup({})).stages
    with pytest.raises(ValueError, match="frozen"):
        stage.hash = "sha256"

"""The validate stage: per-line outcomes, output/quarantine files, thresholds and logging."""

import json
import logging
from pathlib import Path

import pytest

from curator.config import ValidateStageSpec
from curator.errors import ConfigError
from curator.errors import InputFileError
from curator.errors import QuarantineThresholdError
from curator.schemas import RecordKind
from curator.stages.validate import Accepted
from curator.stages.validate import Outcome
from curator.stages.validate import Quarantined
from curator.stages.validate import run_validate
from curator.stages.validate import validate_lines

from ..conftest import JsonLogLines

GOOD = {"id": "ok-1", "prompt": "What is 2+2?", "response": "4"}
GOOD_2 = {"id": "ok-2", "messages": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}]}
BAD_SCHEMA = {"id": "bad-1", "prompt": "What is 2+2?", "response": " "}


def line(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode()


def outcomes(*contents: bytes, kind: RecordKind = "sft", config: ValidateStageSpec | None = None) -> list[Outcome]:
    numbered = list(enumerate(contents, start=1))
    return list(validate_lines(numbered, kind=kind, config=config or ValidateStageSpec()))


def reasons(outcome: Outcome) -> list[tuple[str, str]]:
    assert isinstance(outcome, Quarantined), outcome
    return [(issue.code, issue.field) for issue in outcome.issues]


def write_jsonl(path: Path, *contents: bytes) -> Path:
    path.write_bytes(b"".join(content + b"\n" for content in contents))
    return path


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# validate_lines: one outcome per non-blank line


def test_valid_record_is_serialised_with_its_id() -> None:
    [outcome] = outcomes(line({"prompt": "q", "response": "a"}))
    assert isinstance(outcome, Accepted)
    assert outcome.line == 1
    assert json.loads(outcome.data) == {"id": outcome.record_id, "prompt": "q", "response": "a"}


def test_blank_lines_are_skipped_but_line_numbers_are_kept() -> None:
    result = outcomes(b"", line(GOOD), b"   \t", line(BAD_SCHEMA))
    assert [o.line for o in result] == [2, 4]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b'{"prompt": "caf\xe9"}', "invalid_encoding"),
        (b'{"prompt": "q", "response": ', "invalid_json"),
        (b"not json at all", "invalid_json"),
        (b'{"prompt": "q", "prompt": "r", "response": "a"}', "duplicate_key"),
        (b'{"prompt": "q", "response": "a", "metadata": {"score": NaN}}', "non_finite_number"),
    ],
)
def test_undecodable_lines_keep_their_raw_text(content: bytes, code: str) -> None:
    [outcome] = outcomes(content)
    assert reasons(outcome) == [(code, "")]
    assert isinstance(outcome, Quarantined)
    assert outcome.raw is not None
    assert "record" not in outcome.to_dict()


def test_invalid_utf8_is_shown_with_escapes() -> None:
    [outcome] = outcomes(b'{"prompt": "caf\xe9"}')
    assert isinstance(outcome, Quarantined)
    assert outcome.raw == '{"prompt": "caf\\xe9"}'
    assert "byte 0xe9 at offset 15" in outcome.issues[0].message


def test_schema_failures_report_every_issue_and_keep_the_record() -> None:
    raw = {"id": "x", "messages": [{"role": "user", "content": ""}, {"role": "bot", "content": "hi"}]}
    [outcome] = outcomes(line(raw))
    assert reasons(outcome) == [("blank_text", "messages[0].content"), ("literal_error", "messages[1].role")]
    assert isinstance(outcome, Quarantined)
    assert outcome.to_dict() == {
        "line": 1,
        "id": "x",
        "reasons": [issue.to_dict() for issue in outcome.issues],
        "record": raw,
    }


@pytest.mark.parametrize("value", [[1, 2], "text", 3, None])
def test_non_object_json(value: object) -> None:
    [outcome] = outcomes(line(value))
    assert reasons(outcome) == [("not_an_object", "")]


def test_duplicate_explicit_ids_keep_the_first_occurrence() -> None:
    first, second = outcomes(line(GOOD), line(GOOD | {"response": "four"}))
    assert isinstance(first, Accepted)
    assert reasons(second) == [("duplicate_id", "id")]
    assert isinstance(second, Quarantined)
    assert "'ok-1' was already used by line 1" in second.issues[0].message


def test_exact_duplicate_content_without_ids_is_caught_across_shapes() -> None:
    chat = {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}
    first, second = outcomes(line(chat), line({"prompt": "q", "response": "a", "metadata": {"src": "other"}}))
    assert isinstance(first, Accepted)
    assert reasons(second) == [("duplicate_id", "")]
    assert isinstance(second, Quarantined)
    assert "matches the content of line 1" in second.issues[0].message


def test_duplicate_ids_can_be_allowed() -> None:
    result = outcomes(line(GOOD), line(GOOD), config=ValidateStageSpec(reject_duplicate_ids=False))
    assert all(isinstance(o, Accepted) for o in result)


def test_a_quarantined_record_does_not_claim_its_id() -> None:
    bad_then_good = outcomes(line(GOOD | {"response": ""}), line(GOOD))
    assert isinstance(bad_then_good[1], Accepted)


def test_lone_surrogates_are_quarantined_instead_of_crashing_the_writer() -> None:
    [outcome] = outcomes(b'{"prompt": "q", "response": "a", "metadata": {"note": "\\ud800"}}')
    assert reasons(outcome) == [("invalid_unicode", "")]


def test_unknown_fields_policy_is_applied() -> None:
    raw = GOOD | {"source": "gsm8k"}
    [rejected] = outcomes(line(raw))
    [accepted] = outcomes(line(raw), config=ValidateStageSpec(unknown_fields="metadata"))

    assert reasons(rejected) == [("extra_forbidden", "source")]
    assert isinstance(accepted, Accepted)
    assert json.loads(accepted.data)["metadata"] == {"source": "gsm8k"}


def test_preference_kind() -> None:
    pair = {"prompt": "q", "chosen": "good", "rejected": "bad"}
    accepted, rejected = outcomes(line(pair), line(pair | {"rejected": "good"}), kind="preference")
    assert isinstance(accepted, Accepted)
    assert reasons(rejected) == [("identical_responses", "")]


# ---------------------------------------------------------------------------
# run_validate: files, report, thresholds


@pytest.fixture
def mixed_input(tmp_path: Path) -> Path:
    return write_jsonl(
        tmp_path / "raw.jsonl",
        line(GOOD),
        line(BAD_SCHEMA),
        b"",
        b"{broken",
        line(GOOD_2),
        line(GOOD),
    )


def test_run_splits_records_into_output_and_quarantine(mixed_input: Path, tmp_path: Path) -> None:
    report = run_validate(mixed_input, tmp_path / "out", kind="sft")

    assert (report.total, report.valid, report.invalid) == (5, 2, 3)
    assert report.invalid_fraction == pytest.approx(0.6)
    assert report.reasons == {"blank_text": 1, "duplicate_id": 1, "invalid_json": 1}
    assert read_jsonl(report.output_path) == [GOOD, GOOD_2]
    quarantine = read_jsonl(report.quarantine_path)
    assert [entry["line"] for entry in quarantine] == [2, 4, 6]
    broken = quarantine[1]
    assert (broken["line"], broken["raw"]) == (4, "{broken")
    assert "record" not in broken
    assert quarantine[0]["record"] == BAD_SCHEMA


def test_report_paths_and_dict(mixed_input: Path, tmp_path: Path) -> None:
    config = ValidateStageSpec(output_file="clean.jsonl", quarantine_file="rejects.jsonl")
    report = run_validate(mixed_input, tmp_path / "out", kind="sft", config=config)

    assert report.output_path == tmp_path / "out" / "clean.jsonl"
    assert report.quarantine_path == tmp_path / "out" / "rejects.jsonl"
    assert json.loads(json.dumps(report.to_dict())) == report.to_dict()
    assert report.to_dict()["invalid"] == 3


def test_rerunning_produces_byte_identical_files(mixed_input: Path, tmp_path: Path) -> None:
    first = run_validate(mixed_input, tmp_path / "a", kind="sft")
    snapshot = (first.output_path.read_bytes(), first.quarantine_path.read_bytes())

    second = run_validate(mixed_input, tmp_path / "a", kind="sft")

    assert (second.output_path.read_bytes(), second.quarantine_path.read_bytes()) == snapshot
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == ["quarantine.jsonl", "validated.jsonl"]


def test_threshold_exceeded_keeps_quarantine_and_withholds_output(mixed_input: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "validated.jsonl"
    stale.write_text("from an earlier run\n", encoding="utf-8")

    with pytest.raises(QuarantineThresholdError) as excinfo:
        run_validate(mixed_input, out, kind="sft", config=ValidateStageSpec(max_invalid_fraction=0.5))

    error = excinfo.value
    assert (error.invalid, error.total, error.max_fraction) == (3, 5, 0.5)
    assert error.exit_code == 65
    assert len(read_jsonl(out / "quarantine.jsonl")) == 3
    assert not stale.exists()
    assert sorted(p.name for p in out.iterdir()) == ["quarantine.jsonl"]


def test_threshold_is_inclusive(tmp_path: Path) -> None:
    source = write_jsonl(tmp_path / "raw.jsonl", line(GOOD), line(BAD_SCHEMA))
    report = run_validate(source, tmp_path / "out", kind="sft", config=ValidateStageSpec(max_invalid_fraction=0.5))
    assert report.invalid_fraction == 0.5
    assert report.output_path.exists()


def test_empty_input_writes_empty_files_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = write_jsonl(tmp_path / "raw.jsonl", b"", b"  ")

    with caplog.at_level(logging.INFO, logger="curator"):
        report = run_validate(source, tmp_path / "out", kind="sft", config=ValidateStageSpec(max_invalid_fraction=0))

    assert (report.total, report.invalid_fraction) == (0, 0.0)
    assert report.output_path.read_bytes() == b""
    assert report.quarantine_path.read_bytes() == b""
    assert "validate.empty_input" in [r.getMessage() for r in caplog.records]


def test_missing_input_fails_before_creating_outputs(tmp_path: Path) -> None:
    with pytest.raises(InputFileError):
        run_validate(tmp_path / "absent.jsonl", tmp_path / "out", kind="sft")
    assert not (tmp_path / "out").exists()


def test_refuses_to_overwrite_its_input(tmp_path: Path) -> None:
    source = write_jsonl(tmp_path / "validated.jsonl", line(GOOD))
    with pytest.raises(ConfigError, match="would overwrite the input"):
        run_validate(source, tmp_path, kind="sft")
    assert read_jsonl(source) == [GOOD]


def test_logs_a_structured_summary_with_the_stage_bound(
    mixed_input: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="curator"):
        run_validate(mixed_input, tmp_path / "out", kind="sft")

    quarantined = [r for r in caplog.records if r.getMessage() == "validate.record_quarantined"]
    assert [(r.__dict__["line"], r.__dict__["codes"]) for r in quarantined] == [
        (2, ["blank_text"]),
        (4, ["invalid_json"]),
        (6, ["duplicate_id"]),
    ]
    [finished] = [r for r in caplog.records if r.getMessage() == "validate.finished"]
    assert (finished.__dict__["total"], finished.__dict__["valid"], finished.__dict__["invalid"]) == (5, 2, 3)


def test_json_logs_carry_the_stage_field(mixed_input: Path, tmp_path: Path, json_log: JsonLogLines) -> None:
    run_validate(mixed_input, tmp_path / "out", kind="sft")

    lines = json_log()
    assert {entry["stage"] for entry in lines} == {"validate"}
    [finished] = [entry for entry in lines if entry["event"] == "validate.finished"]
    assert finished["reasons"] == {"blank_text": 1, "duplicate_id": 1, "invalid_json": 1}
    assert finished["level"] == "info"

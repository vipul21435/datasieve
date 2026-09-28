"""parse_record: shape detection, ids, unknown-field policy and serialisation."""

import copy
import re

import pytest
from pydantic import ValidationError

from curator.errors import RecordValidationError
from curator.schemas import RECORD_KINDS
from curator.schemas import PreferenceTextRecord
from curator.schemas import RecordKind
from curator.schemas import SFTPromptResponseRecord
from curator.schemas import parse_record
from curator.schemas.common import CONTENT_ID_LENGTH
from curator.schemas.common import MAX_ID_LENGTH

from .helpers import issue_codes

SFT = {"prompt": "What is 2+2?", "response": "4"}


@pytest.mark.parametrize(
    ("raw", "json_type"),
    [([SFT], "array"), ("text", "string"), (3, "number"), (None, "null"), (True, "boolean")],
)
def test_non_objects_are_rejected_with_their_json_type(raw: object, json_type: str) -> None:
    with pytest.raises(RecordValidationError) as excinfo:
        parse_record(raw, "sft")
    [issue] = excinfo.value.issues
    assert (issue.code, issue.field) == ("not_an_object", "")
    assert issue.message.endswith(f"got {json_type}")


@pytest.mark.parametrize("kind", RECORD_KINDS)
def test_unrecognised_shape(kind: RecordKind) -> None:
    assert issue_codes({"text": "raw pretraining text"}, kind) == [("unknown_record_shape", "")]


def test_preference_record_is_not_a_valid_sft_record() -> None:
    raw = {"prompt": "q", "chosen": "a", "rejected": "b"}
    assert sorted(issue_codes(raw, "sft")) == [
        ("extra_forbidden", "chosen"),
        ("extra_forbidden", "rejected"),
        ("missing", "response"),
    ]


def test_unknown_fields_are_rejected_by_default() -> None:
    assert issue_codes(SFT | {"source": "gsm8k"}, "sft") == [("extra_forbidden", "source")]


def test_unknown_fields_can_move_into_metadata() -> None:
    raw = SFT | {"source": "gsm8k", "lang": "en", "metadata": {"lang": "en-US", "split": "train"}}
    before = copy.deepcopy(raw)

    record = parse_record(raw, "sft", unknown_fields="metadata")

    # Explicit metadata wins on key clashes; the caller's dict is not mutated.
    assert record.metadata == {"source": "gsm8k", "lang": "en-US", "split": "train"}
    assert raw == before


def test_moving_unknown_fields_does_not_hide_a_bad_metadata_value() -> None:
    raw = SFT | {"source": "x", "metadata": "not an object"}
    assert sorted(issue_codes(raw, "sft", unknown_fields="metadata")) == [
        ("dict_type", "metadata"),
        ("extra_forbidden", "source"),
    ]


@pytest.mark.parametrize(("raw_id", "expected"), [("ex-001", "ex-001"), (7, "7"), ("oasst/2023:abc", "oasst/2023:abc")])
def test_explicit_ids(raw_id: object, expected: str) -> None:
    record = parse_record(SFT | {"id": raw_id}, "sft")
    assert record.id == expected
    assert record.record_id == expected


@pytest.mark.parametrize(
    ("raw_id", "code"),
    [
        ("", "invalid_id"),
        ("has space", "invalid_id"),
        ("x" * (MAX_ID_LENGTH + 1), "invalid_id"),
        (True, "string_type"),
        (1.5, "string_type"),
    ],
)
def test_invalid_ids(raw_id: object, code: str) -> None:
    assert issue_codes(SFT | {"id": raw_id}, "sft") == [(code, "id")]


def test_content_id_is_stable_hex_and_ignores_metadata_and_key_order() -> None:
    record = parse_record(SFT, "sft")
    reordered = parse_record({"response": "4", "prompt": "What is 2+2?", "metadata": {"source": "x"}}, "sft")

    assert re.fullmatch(rf"[0-9a-f]{{{CONTENT_ID_LENGTH}}}", record.record_id)
    assert record.record_id == reordered.record_id
    assert record.record_id != parse_record(SFT | {"response": "5"}, "sft").record_id


def test_explicit_id_takes_precedence_over_content_id() -> None:
    record = parse_record(SFT | {"id": "given"}, "sft")
    assert record.record_id == "given"
    assert record.content_id() != "given"


def test_with_record_id_fills_the_id_once() -> None:
    record = parse_record(SFT, "sft")
    filled = record.with_record_id()
    assert record.id is None
    assert filled.id == record.content_id()
    assert filled.with_record_id() is filled


def test_to_json_dict_mirrors_the_input_key_order_with_id_first() -> None:
    raw = {"metadata": {"source": "x"}, "response": "4", "prompt": "What is 2+2?"}
    record = parse_record(raw, "sft").with_record_id()

    out = record.to_json_dict()

    assert list(out) == ["id", "metadata", "response", "prompt"]
    assert out == {"id": record.content_id(), **raw}


def test_preference_round_trip_is_unchanged_apart_from_the_id() -> None:
    raw = {"prompt": "q", "chosen": "a", "rejected": "b", "score_chosen": 1.5, "score_rejected": 0.5, "id": "p-1"}
    out = parse_record(raw, "preference").to_json_dict()
    assert list(out) == ["id", "prompt", "chosen", "rejected", "score_chosen", "score_rejected"]
    assert out == raw


def test_moved_unknown_fields_serialise_inside_metadata() -> None:
    record = parse_record(SFT | {"source": "gsm8k"}, "sft", unknown_fields="metadata")
    assert record.to_json_dict() == SFT | {"metadata": {"source": "gsm8k"}}


def test_defaults_are_not_added_to_the_output() -> None:
    assert parse_record(SFT, "sft").to_json_dict() == SFT


def test_records_are_immutable() -> None:
    record = parse_record({"prompt": "q", "chosen": "a", "rejected": "b"}, "preference")
    assert isinstance(record, PreferenceTextRecord)
    with pytest.raises(ValidationError):
        record.chosen = "c"


def test_models_can_be_built_directly_from_python() -> None:
    record = SFTPromptResponseRecord(prompt="q", response="a", metadata={"n": [1, 2, {"k": None}]})
    assert record.metadata == {"n": [1, 2, {"k": None}]}

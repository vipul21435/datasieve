"""Preference (chosen/rejected) record schemas: text and chat shapes."""

import math

import pytest

from curator.schemas import PreferenceChatRecord
from curator.schemas import PreferenceTextRecord
from curator.schemas import parse_record

from .helpers import issue_codes
from .helpers import msg

TEXT_PAIR = {"prompt": "Capital of France?", "chosen": "Paris.", "rejected": "Lyon."}


def chat_pair(prompt_roles: list[str], chosen_roles: list[str], rejected_roles: list[str]) -> dict[str, object]:
    return {
        "prompt": [msg(role, f"p{i}") for i, role in enumerate(prompt_roles)],
        "chosen": [msg(role, f"good{i}") for i, role in enumerate(chosen_roles)],
        "rejected": [msg(role, f"bad{i}") for i, role in enumerate(rejected_roles)],
    }


def test_text_pair() -> None:
    record = parse_record(TEXT_PAIR, "preference")
    assert isinstance(record, PreferenceTextRecord)
    assert record.chosen == "Paris."


def test_chat_pair_with_multi_turn_prompt_and_tool_use() -> None:
    raw = chat_pair(["system", "user", "assistant", "user"], ["assistant", "tool", "assistant"], ["assistant"])
    record = parse_record(raw, "preference")
    assert isinstance(record, PreferenceChatRecord)
    assert [m.role for m in record.chosen] == ["assistant", "tool", "assistant"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (chat_pair(["user", "assistant"], ["assistant"], ["assistant"]), [("last_turn_not_user", "prompt")]),
        (chat_pair(["user"], ["user", "assistant"], ["assistant"]), [("first_turn_not_assistant", "chosen")]),
        (chat_pair(["user"], ["assistant"], ["system", "assistant"]), [("misplaced_system_message", "rejected")]),
        (chat_pair(["user"], ["assistant", "assistant"], ["assistant"]), [("consecutive_same_role", "chosen")]),
        (chat_pair([], ["assistant"], ["assistant"]), [("too_short", "prompt")]),
    ],
)
def test_chat_pair_turn_order(raw: dict[str, object], expected: list[tuple[str, str]]) -> None:
    assert issue_codes(raw, "preference") == expected


def test_mixed_text_and_chat_fields_are_rejected() -> None:
    raw = {"prompt": [msg("user")], "chosen": "Paris.", "rejected": [msg("assistant")]}
    assert issue_codes(raw, "preference") == [("list_type", "chosen")]


@pytest.mark.parametrize(
    "rejected",
    ["Paris.", "  Paris. ", "Paris.\n"],
)
def test_identical_responses_carry_no_preference(rejected: str) -> None:
    assert issue_codes(TEXT_PAIR | {"rejected": rejected}, "preference") == [("identical_responses", "")]


def test_identical_chat_continuations_are_rejected() -> None:
    raw = {"prompt": [msg("user", "q")], "chosen": [msg("assistant", "a")], "rejected": [msg("assistant", "a ")]}
    assert issue_codes(raw, "preference") == [("identical_responses", "")]


@pytest.mark.parametrize(("chosen", "rejected"), [(8.5, 3), (4, 4), (0, -1.5)])
def test_scores_that_agree_with_the_labels(chosen: float, rejected: float) -> None:
    record = parse_record(TEXT_PAIR | {"score_chosen": chosen, "score_rejected": rejected}, "preference")
    assert isinstance(record, PreferenceTextRecord)
    assert record.score_chosen == chosen


def test_chosen_scored_below_rejected_looks_mislabelled() -> None:
    raw = TEXT_PAIR | {"score_chosen": 2, "score_rejected": 7.5}
    assert issue_codes(raw, "preference") == [("chosen_scored_below_rejected", "")]


@pytest.mark.parametrize(
    ("score", "code"),
    [("9", "float_type"), (True, "float_type"), (math.nan, "finite_number"), (math.inf, "finite_number")],
)
def test_scores_must_be_finite_numbers(score: object, code: str) -> None:
    assert issue_codes(TEXT_PAIR | {"score_chosen": score}, "preference") == [(code, "score_chosen")]


def test_one_score_alone_is_allowed() -> None:
    record = parse_record(TEXT_PAIR | {"score_rejected": 1.0}, "preference")
    assert record.to_json_dict()["score_rejected"] == 1.0


def test_text_and_chat_forms_share_a_content_id() -> None:
    text = parse_record(TEXT_PAIR, "preference")
    chat = parse_record(
        {
            "prompt": [msg("user", "Capital of France?")],
            "chosen": [msg("assistant", "Paris.")],
            "rejected": [msg("assistant", "Lyon.")],
        },
        "preference",
    )
    assert text.content_id() == chat.content_id()


def test_scores_do_not_change_the_content_id() -> None:
    plain = parse_record(TEXT_PAIR, "preference")
    scored = parse_record(TEXT_PAIR | {"score_chosen": 1, "score_rejected": 0}, "preference")
    assert plain.content_id() == scored.content_id()


def test_swapping_chosen_and_rejected_changes_the_content_id() -> None:
    swapped = TEXT_PAIR | {"chosen": TEXT_PAIR["rejected"], "rejected": TEXT_PAIR["chosen"]}
    assert parse_record(TEXT_PAIR, "preference").content_id() != parse_record(swapped, "preference").content_id()

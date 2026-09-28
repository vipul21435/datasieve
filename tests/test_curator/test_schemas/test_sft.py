"""SFT record schemas: chat and prompt/response shapes."""

import pytest

from curator.errors import RecordValidationError
from curator.schemas import Message
from curator.schemas import SFTChatRecord
from curator.schemas import SFTPromptResponseRecord
from curator.schemas import parse_record

from .helpers import issue_codes
from .helpers import msg


@pytest.mark.parametrize(
    "roles",
    [
        ["user", "assistant"],
        ["system", "user", "assistant"],
        ["user", "assistant", "user", "assistant"],
        ["user", "assistant", "tool", "assistant"],
        ["user", "assistant", "tool", "tool", "assistant"],
    ],
)
def test_valid_conversations(roles: list[str]) -> None:
    record = parse_record({"messages": [msg(role) for role in roles]}, "sft")
    assert isinstance(record, SFTChatRecord)
    assert [m.role for m in record.messages] == roles


@pytest.mark.parametrize(
    ("roles", "code"),
    [
        ([], "too_short"),
        (["user"], "too_short"),
        (["assistant", "user", "assistant"], "first_turn_not_user"),
        (["system", "assistant"], "first_turn_not_user"),
        (["system", "system"], "misplaced_system_message"),
        (["user", "system", "assistant"], "misplaced_system_message"),
        (["user", "user", "assistant"], "consecutive_same_role"),
        (["user", "assistant", "assistant"], "consecutive_same_role"),
        (["user", "tool", "assistant"], "orphan_tool_message"),
        (["user", "assistant", "user"], "last_turn_not_assistant"),
        (["user", "assistant", "tool"], "last_turn_not_assistant"),
    ],
)
def test_turn_order_rules(roles: list[str], code: str) -> None:
    assert issue_codes({"messages": [msg(role) for role in roles]}, "sft") == [(code, "messages")]


def test_turn_order_message_points_at_the_offending_turn() -> None:
    roles = ["user", "assistant", "user", "user", "assistant"]
    with pytest.raises(RecordValidationError, match="messages 2 and 3 both have role 'user'"):
        parse_record({"messages": [msg(role) for role in roles]}, "sft")


def test_every_bad_message_is_reported() -> None:
    raw = {
        "messages": [
            {"role": "human", "content": "hi"},
            {"role": "assistant", "content": "   \n"},
            {"role": "user", "content": 42},
            {"role": "assistant", "content": "ok", "weight": 1},
        ]
    }
    assert issue_codes(raw, "sft") == [
        ("literal_error", "messages[0].role"),
        ("blank_text", "messages[1].content"),
        ("string_type", "messages[2].content"),
        ("extra_forbidden", "messages[3].weight"),
    ]


def test_optional_message_name_is_kept() -> None:
    record = parse_record({"messages": [msg("user") | {"name": "alice"}, msg("assistant")]}, "sft")
    assert isinstance(record, SFTChatRecord)
    assert record.messages[0].name == "alice"
    assert record.to_json_dict()["messages"] == [msg("user") | {"name": "alice"}, msg("assistant")]


def test_prompt_response_record_and_chat_form() -> None:
    record = parse_record({"system": "Be brief.", "prompt": "2+2?", "response": "4"}, "sft")
    assert isinstance(record, SFTPromptResponseRecord)
    assert record.to_messages() == [
        Message(role="system", content="Be brief."),
        Message(role="user", content="2+2?"),
        Message(role="assistant", content="4"),
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"prompt": "hi"}, [("missing", "response")]),
        ({"prompt": " ", "response": "x"}, [("blank_text", "prompt")]),
        ({"prompt": "hi", "response": ["x"]}, [("string_type", "response")]),
        ({"prompt": "hi", "response": "x", "system": ""}, [("blank_text", "system")]),
    ],
)
def test_prompt_response_errors(raw: dict[str, object], expected: list[tuple[str, str]]) -> None:
    assert issue_codes(raw, "sft") == expected


def test_text_is_never_modified() -> None:
    record = parse_record({"prompt": "  padded prompt\n", "response": "\tresponse  "}, "sft")
    assert isinstance(record, SFTPromptResponseRecord)
    assert (record.prompt, record.response) == ("  padded prompt\n", "\tresponse  ")


def test_chat_and_prompt_response_forms_share_a_content_id() -> None:
    chat = parse_record({"messages": [msg("system", "S"), msg("user", "P"), msg("assistant", "R")]}, "sft")
    flat = parse_record({"system": "S", "prompt": "P", "response": "R"}, "sft")
    assert chat.content_id() == flat.content_id()

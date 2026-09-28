"""Named text fields of each record shape."""

from curator.schemas import PreferenceChatRecord
from curator.schemas import PreferenceTextRecord
from curator.schemas import SFTChatRecord
from curator.schemas import SFTPromptResponseRecord
from curator.schemas import parse_record
from curator.schemas.fields import TEXT_FIELDS
from curator.schemas.fields import record_texts


def test_sft_prompt_response_record() -> None:
    record = SFTPromptResponseRecord(prompt="2+2?", response="4")
    assert record_texts(record) == {"prompt": "2+2?", "response": "4"}


def test_sft_chat_record_joins_everything_before_the_final_turn() -> None:
    record = SFTChatRecord.model_validate({
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "And 3+3?"},
            {"role": "assistant", "content": "6"},
        ]
    })
    assert record_texts(record) == {"prompt": "Be brief.\n2+2?\n4\nAnd 3+3?", "response": "6"}


def test_chat_and_flat_forms_of_one_example_give_the_same_texts() -> None:
    flat = parse_record({"system": "Be brief.", "prompt": "2+2?", "response": "4"}, "sft")
    chat = parse_record(
        {
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "2+2?"},
                {"role": "assistant", "content": "4"},
            ]
        },
        "sft",
    )
    assert record_texts(flat) == record_texts(chat) == {"prompt": "Be brief.\n2+2?", "response": "4"}


def test_preference_text_record() -> None:
    record = PreferenceTextRecord(prompt="q", chosen="good", rejected="bad")
    assert record_texts(record) == {"prompt": "q", "chosen": "good", "rejected": "bad"}


def test_preference_chat_record_joins_each_part() -> None:
    record = PreferenceChatRecord.model_validate({
        "prompt": [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
        "chosen": [{"role": "assistant", "content": "good"}],
        "rejected": [{"role": "assistant", "content": "bad"}],
    })
    assert record_texts(record) == {"prompt": "q1\na1\nq2", "chosen": "good", "rejected": "bad"}


def test_text_fields_match_what_records_expose() -> None:
    sft = record_texts(SFTPromptResponseRecord(prompt="q", response="a"))
    preference = record_texts(PreferenceTextRecord(prompt="q", chosen="good", rejected="bad"))
    assert tuple(sft) == TEXT_FIELDS["sft"]
    assert tuple(preference) == TEXT_FIELDS["preference"]

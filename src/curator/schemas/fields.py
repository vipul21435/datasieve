"""Named text fields of a record, for stages that work on text rather than on structure.

SFT records expose ``prompt`` (everything before the final assistant turn, in
either input shape) and ``response`` (that final turn). Preference records
expose ``prompt``, ``chosen`` and ``rejected``. Multi-message parts are joined
with newlines, so the chat and flat forms of one example give the same text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from typing import Literal

from curator.schemas.common import Message
from curator.schemas.parse import Record
from curator.schemas.parse import RecordKind
from curator.schemas.sft import SFTChatRecord
from curator.schemas.sft import SFTPromptResponseRecord

TextField = Literal["prompt", "response", "chosen", "rejected"]

TEXT_FIELDS: Final[dict[RecordKind, tuple[TextField, ...]]] = {
    "sft": ("prompt", "response"),
    "preference": ("prompt", "chosen", "rejected"),
}
"""The text fields each record kind has, in their natural order."""


def _join(messages: Sequence[Message]) -> str:
    return "\n".join(message.content for message in messages)


def record_texts(record: Record) -> dict[TextField, str]:
    """The record's text fields (see :data:`TEXT_FIELDS` for the ones each kind has).

    >>> record_texts(SFTPromptResponseRecord(system="Be brief.", prompt="2+2?", response="4"))
    {'prompt': 'Be brief.\\n2+2?', 'response': '4'}
    """
    if isinstance(record, SFTChatRecord | SFTPromptResponseRecord):
        messages = record.to_messages()
        return {"prompt": _join(messages[:-1]), "response": messages[-1].content}
    return {
        "prompt": _join(record.prompt_messages()),
        "chosen": _join(record.chosen_messages()),
        "rejected": _join(record.rejected_messages()),
    }


__all__ = ["TEXT_FIELDS", "TextField", "record_texts"]

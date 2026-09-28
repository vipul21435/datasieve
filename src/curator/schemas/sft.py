"""Schemas for supervised fine-tuning (SFT) records.

Two input shapes are accepted:

* chat: ``{"messages": [{"role": "user", "content": ...}, {"role": "assistant", ...}]}``
* prompt/response: ``{"prompt": ..., "response": ..., "system": ...optional}``

Both may carry an optional ``id`` and a free-form ``metadata`` object.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic import JsonValue
from pydantic import field_validator

from curator.schemas.common import Message
from curator.schemas.common import RecordBase
from curator.schemas.common import Text
from curator.schemas.common import check_turn_order
from curator.schemas.common import dump_messages


class SFTChatRecord(RecordBase):
    """A multi-turn conversation that ends with the assistant turn to train on.

    >>> record = SFTChatRecord.model_validate(
    ...     {"messages": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}]}
    ... )
    >>> [m.role for m in record.messages]
    ['user', 'assistant']
    """

    messages: Annotated[list[Message], Field(min_length=2)]

    @field_validator("messages")
    @classmethod
    def _turn_order(cls, messages: list[Message]) -> list[Message]:
        check_turn_order(messages, first="user", last="assistant", allow_system=True)
        return messages

    def to_messages(self) -> list[Message]:
        return list(self.messages)

    def canonical_content(self) -> dict[str, JsonValue]:
        return {"messages": dump_messages(self.to_messages())}


class SFTPromptResponseRecord(RecordBase):
    """A single-turn example: an optional system prompt, a prompt and the target response."""

    system: Text | None = None
    prompt: Text
    response: Text

    def to_messages(self) -> list[Message]:
        """The same example in chat form.

        >>> SFTPromptResponseRecord(prompt="2+2?", response="4").to_messages()[1].role
        'assistant'
        """
        messages = [Message(role="system", content=self.system)] if self.system is not None else []
        messages += [Message(role="user", content=self.prompt), Message(role="assistant", content=self.response)]
        return messages

    def canonical_content(self) -> dict[str, JsonValue]:
        # Same shape as SFTChatRecord, so a prompt/response record and its chat form share a content id.
        return {"messages": dump_messages(self.to_messages())}


SFTRecord = SFTChatRecord | SFTPromptResponseRecord

__all__ = ["SFTChatRecord", "SFTPromptResponseRecord", "SFTRecord"]

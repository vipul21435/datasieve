"""Schemas for preference (chosen/rejected) records, as used for DPO, RLHF reward models, etc.

Two input shapes are accepted, matching the common TRL/Hugging Face layouts:

* text: ``{"prompt": str, "chosen": str, "rejected": str}``
* chat: ``{"prompt": [messages ending in a user turn],
  "chosen": [assistant continuation], "rejected": [assistant continuation]}``

Either shape may add ``score_chosen`` / ``score_rejected`` (for example reward
model or annotator ratings), an ``id`` and ``metadata``.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Annotated
from typing import Self

from pydantic import Field
from pydantic import FiniteFloat
from pydantic import JsonValue
from pydantic import field_validator
from pydantic import model_validator
from pydantic_core import PydanticCustomError

from curator.schemas.common import Message
from curator.schemas.common import RecordBase
from curator.schemas.common import Text
from curator.schemas.common import check_turn_order
from curator.schemas.common import dump_messages


def _normalised(messages: list[Message]) -> list[tuple[str, str]]:
    return [(message.role, " ".join(message.content.split())) for message in messages]


class _PreferenceBase(RecordBase):
    score_chosen: FiniteFloat | None = None
    score_rejected: FiniteFloat | None = None

    @abstractmethod
    def prompt_messages(self) -> list[Message]: ...

    @abstractmethod
    def chosen_messages(self) -> list[Message]: ...

    @abstractmethod
    def rejected_messages(self) -> list[Message]: ...

    @model_validator(mode="after")
    def _pair_is_informative(self) -> Self:
        # Whitespace-only differences do not make two responses different training signals.
        if _normalised(self.chosen_messages()) == _normalised(self.rejected_messages()):
            raise PydanticCustomError(
                "identical_responses",
                "chosen and rejected are identical (ignoring whitespace), so the pair carries no preference",
            )
        if (
            self.score_chosen is not None
            and self.score_rejected is not None
            and self.score_chosen < self.score_rejected
        ):
            raise PydanticCustomError(
                "chosen_scored_below_rejected",
                "score_chosen ({chosen}) is lower than score_rejected ({rejected}); the pair looks mislabelled",
                {"chosen": self.score_chosen, "rejected": self.score_rejected},
            )
        return self

    def canonical_content(self) -> dict[str, JsonValue]:
        # Text and chat forms of the same pair share a content id. Scores are labels, not content.
        return {
            "prompt": dump_messages(self.prompt_messages()),
            "chosen": dump_messages(self.chosen_messages()),
            "rejected": dump_messages(self.rejected_messages()),
        }


class PreferenceTextRecord(_PreferenceBase):
    """A prompt with a preferred and a dispreferred plain-text response.

    >>> PreferenceTextRecord(prompt="Capital of France?", chosen="Paris.", rejected="Lyon.").chosen
    'Paris.'
    """

    prompt: Text
    chosen: Text
    rejected: Text

    def prompt_messages(self) -> list[Message]:
        return [Message(role="user", content=self.prompt)]

    def chosen_messages(self) -> list[Message]:
        return [Message(role="assistant", content=self.chosen)]

    def rejected_messages(self) -> list[Message]:
        return [Message(role="assistant", content=self.rejected)]


class PreferenceChatRecord(_PreferenceBase):
    """A conversation prefix with two alternative assistant continuations."""

    prompt: Annotated[list[Message], Field(min_length=1)]
    chosen: Annotated[list[Message], Field(min_length=1)]
    rejected: Annotated[list[Message], Field(min_length=1)]

    @field_validator("prompt")
    @classmethod
    def _prompt_turn_order(cls, messages: list[Message]) -> list[Message]:
        check_turn_order(messages, first="user", last="user", allow_system=True)
        return messages

    @field_validator("chosen", "rejected")
    @classmethod
    def _continuation_turn_order(cls, messages: list[Message]) -> list[Message]:
        check_turn_order(messages, first="assistant", last="assistant", allow_system=False)
        return messages

    def prompt_messages(self) -> list[Message]:
        return list(self.prompt)

    def chosen_messages(self) -> list[Message]:
        return list(self.chosen)

    def rejected_messages(self) -> list[Message]:
        return list(self.rejected)


PreferenceRecord = PreferenceTextRecord | PreferenceChatRecord

__all__ = ["PreferenceChatRecord", "PreferenceRecord", "PreferenceTextRecord"]

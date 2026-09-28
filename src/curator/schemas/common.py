"""Building blocks shared by the SFT and preference record schemas.

Validation is *strict*: values are never coerced (``"1"`` is not a number,
``1`` is not a string) and unknown keys are rejected, because silently
"fixing" training data hides upstream bugs. Text is never modified either;
the validator only decides whether a record is usable as-is.
"""

from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from collections.abc import Sequence
from itertools import pairwise
from typing import Annotated
from typing import Literal
from typing import Self

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import BeforeValidator
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import ModelWrapValidatorHandler
from pydantic import PrivateAttr
from pydantic import model_validator
from pydantic_core import PydanticCustomError

Role = Literal["system", "user", "assistant", "tool"]
TurnRole = Literal["user", "assistant"]

MAX_ID_LENGTH = 256
# Hex characters kept from the SHA-256 content hash; 64 bits is ample for dataset-sized collections.
CONTENT_ID_LENGTH = 16


def _require_text(value: str) -> str:
    if not value.strip():
        raise PydanticCustomError("blank_text", "must contain non-whitespace text")
    return value


def _id_from_int(value: object) -> object:
    # Integer ids are common in public datasets; store them as strings. bool is an int subclass, keep it out.
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


def _check_id(value: str) -> str:
    if not value or len(value) > MAX_ID_LENGTH or any(char.isspace() for char in value):
        raise PydanticCustomError(
            "invalid_id",
            "id must be 1-{max_length} characters with no whitespace",
            {"max_length": MAX_ID_LENGTH},
        )
    return value


Text = Annotated[str, AfterValidator(_require_text)]
"""A string with at least one non-whitespace character. Surrounding whitespace is kept."""

RecordId = Annotated[str, BeforeValidator(_id_from_int), AfterValidator(_check_id)]
"""A caller-supplied record id: a non-empty string without whitespace, or an integer."""

Metadata = dict[str, JsonValue]


class StrictModel(BaseModel):
    """Base for curator's data models: strict types, no unknown keys, immutable."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Message(StrictModel):
    """One chat turn."""

    role: Role
    content: Text
    name: Annotated[str, Field(min_length=1)] | None = None


def check_turn_order(
    messages: Sequence[Message],
    *,
    first: TurnRole,
    last: TurnRole,
    allow_system: bool,
) -> None:
    """Raise a ``PydanticCustomError`` for the first turn-order rule ``messages`` breaks.

    Rules, checked in this order:

    * ``system`` may only appear as the very first message, and only when ``allow_system``;
    * the first non-system message has role ``first``;
    * ``user`` and ``assistant`` alternate: never two ``user`` or two ``assistant`` turns in a row;
    * a ``tool`` message directly follows an ``assistant`` or another ``tool`` message;
    * the last message has role ``last``.
    """
    for index, message in enumerate(messages):
        if message.role == "system" and (index != 0 or not allow_system):
            raise PydanticCustomError(
                "misplaced_system_message",
                "message {index} has role 'system'; only the first message of a conversation may be a system message",
                {"index": index},
            )

    turns = list(enumerate(messages))
    if turns and turns[0][1].role == "system":
        turns = turns[1:]
    if not turns:
        raise PydanticCustomError("no_turns", "conversation has no user or assistant messages")

    if turns[0][1].role != first:
        raise PydanticCustomError(
            "first_turn_not_user" if first == "user" else "first_turn_not_assistant",
            "the first turn must have role '{expected}', got '{role}'",
            {"expected": first, "role": turns[0][1].role},
        )

    for (previous_index, previous), (index, current) in pairwise(turns):
        if current.role == "tool" and previous.role not in ("assistant", "tool"):
            raise PydanticCustomError(
                "orphan_tool_message",
                "message {index} has role 'tool' but follows a '{previous}' message instead of an assistant turn",
                {"index": index, "previous": previous.role},
            )
        if current.role == previous.role and current.role in ("user", "assistant"):
            raise PydanticCustomError(
                "consecutive_same_role",
                "messages {previous_index} and {index} both have role '{role}'; user and assistant turns must alternate",
                {"previous_index": previous_index, "index": index, "role": current.role},
            )

    if messages[-1].role != last:
        raise PydanticCustomError(
            "last_turn_not_user" if last == "user" else "last_turn_not_assistant",
            "the last turn must have role '{expected}', got '{role}'",
            {"expected": last, "role": messages[-1].role},
        )


def dump_messages(messages: Sequence[Message]) -> list[JsonValue]:
    """Messages as plain dicts in one canonical shape (unset optional keys omitted)."""
    return [message.model_dump(mode="json", exclude_none=True) for message in messages]


class RecordBase(StrictModel):
    """Fields and identity shared by every record type.

    ``id`` is optional in the input. :attr:`record_id` falls back to a hash of
    the training content (never of ``metadata``), so the same example gets the
    same id on every run and in every equivalent format.
    """

    id: RecordId | None = None
    metadata: Metadata = Field(default_factory=dict)

    # Key order of the input mapping, so serialisation can mirror it.
    _input_keys: tuple[str, ...] = PrivateAttr(default=())

    @model_validator(mode="wrap")
    @classmethod
    def _remember_key_order(cls, data: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        record = handler(data)
        if isinstance(data, dict):
            record._input_keys = tuple(str(key) for key in data)
        return record

    @abstractmethod
    def canonical_content(self) -> dict[str, JsonValue]:
        """The training content in one normalised shape, used for the content id."""

    def content_id(self) -> str:
        payload = json.dumps(self.canonical_content(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        # surrogatepass: lone surrogates are rejected later by the validator stage, but hashing must not crash on them.
        return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:CONTENT_ID_LENGTH]

    @property
    def record_id(self) -> str:
        """The caller-supplied id, or the content-derived one when none was given."""
        return self.id if self.id is not None else self.content_id()

    def with_record_id(self) -> Self:
        """Return a copy whose ``id`` is filled in with :attr:`record_id`."""
        return self if self.id is not None else self.model_copy(update={"id": self.content_id()})

    def to_json_dict(self) -> dict[str, JsonValue]:
        """Serialise for JSONL output in the input's shape.

        Only fields the input set are written (no ``metadata: {}`` or
        ``name: null`` is added), in the input's key order, with ``id`` first.
        A record read from JSON and written back is therefore unchanged apart
        from a filled-in ``id``.
        """
        data = self.model_dump(mode="json", exclude_unset=True)
        position = {key: index for index, key in enumerate(self._input_keys)}
        return dict(sorted(data.items(), key=lambda item: (item[0] != "id", position.get(item[0], len(position)))))


__all__ = [
    "CONTENT_ID_LENGTH",
    "MAX_ID_LENGTH",
    "Message",
    "Metadata",
    "RecordBase",
    "RecordId",
    "Role",
    "StrictModel",
    "Text",
    "TurnRole",
    "check_turn_order",
    "dump_messages",
]

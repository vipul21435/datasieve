"""Turn a decoded JSON value into a typed record, or explain why it cannot be one.

The record *kind* (``sft`` or ``preference``) comes from the pipeline spec; the
*shape* within a kind (chat vs prompt/response, text vs chat preference) is
detected per record from its keys, so one file may mix shapes of the same kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final
from typing import Literal

from pydantic import ValidationError

from curator.errors import RecordValidationError
from curator.schemas.issues import RecordIssue
from curator.schemas.issues import issues_from_validation_error
from curator.schemas.preference import PreferenceChatRecord
from curator.schemas.preference import PreferenceRecord
from curator.schemas.preference import PreferenceTextRecord
from curator.schemas.sft import SFTChatRecord
from curator.schemas.sft import SFTPromptResponseRecord
from curator.schemas.sft import SFTRecord

RecordKind = Literal["sft", "preference"]
UnknownFieldPolicy = Literal["reject", "metadata"]
"""What to do with top-level keys a schema does not define: reject the record, or move them into ``metadata``."""

Record = SFTRecord | PreferenceRecord

RECORD_KINDS: Final[tuple[RecordKind, ...]] = ("sft", "preference")

_PREFERENCE_KEYS: Final = ("prompt", "chosen", "rejected")
_EXPECTED_SHAPE: Final[dict[RecordKind, str]] = {
    "sft": "a 'messages' list (chat) or 'prompt' and 'response' strings",
    "preference": "'prompt', 'chosen' and 'rejected' (all strings, or all message lists)",
}


def select_model(raw: Mapping[str, object], kind: RecordKind) -> type[Record] | None:
    """Pick the schema for ``raw`` from its keys, or ``None`` if it matches no shape of ``kind``.

    >>> select_model({"messages": []}, "sft").__name__
    'SFTChatRecord'
    >>> select_model({"prompt": [], "chosen": [], "rejected": []}, "preference").__name__
    'PreferenceChatRecord'
    >>> select_model({"text": "hello"}, "sft") is None
    True
    """
    if kind == "sft":
        if "messages" in raw:
            return SFTChatRecord
        if "prompt" in raw or "response" in raw:
            return SFTPromptResponseRecord
        return None
    if any(isinstance(raw.get(key), list) for key in _PREFERENCE_KEYS):
        return PreferenceChatRecord
    if any(key in raw for key in _PREFERENCE_KEYS):
        return PreferenceTextRecord
    return None


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _move_unknown_fields(raw: Mapping[str, object], model: type[Record]) -> Mapping[str, object]:
    known = model.model_fields.keys()
    unknown = {key: value for key, value in raw.items() if key not in known}
    metadata = raw.get("metadata", {})
    if not unknown or not isinstance(metadata, dict):
        # Nothing to move, or metadata is not an object: let schema validation report it as-is.
        return raw
    kept = {key: value for key, value in raw.items() if key in known}
    # On a key clash the record's explicit metadata wins over the moved field.
    kept["metadata"] = {**unknown, **metadata}
    return kept


def parse_record(raw: object, kind: RecordKind, *, unknown_fields: UnknownFieldPolicy = "reject") -> Record:
    """Validate one decoded JSON value as a record of ``kind``.

    Raises :class:`~curator.errors.RecordValidationError` listing every issue found.

    >>> type(parse_record({"prompt": "Hi", "response": "Hello"}, "sft")).__name__
    'SFTPromptResponseRecord'
    >>> parse_record({"prompt": "Hi"}, "sft")
    Traceback (most recent call last):
    ...
    curator.errors.RecordValidationError: response: Field required [missing]
    """
    if not isinstance(raw, dict):
        raise RecordValidationError([
            RecordIssue("not_an_object", "", f"a record must be a JSON object, got {_json_type(raw)}")
        ])
    model = select_model(raw, kind)
    if model is None:
        raise RecordValidationError([
            RecordIssue("unknown_record_shape", "", f"not a {kind} record: expected {_EXPECTED_SHAPE[kind]}")
        ])
    data = _move_unknown_fields(raw, model) if unknown_fields == "metadata" else raw
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise RecordValidationError(issues_from_validation_error(exc)) from None


__all__ = ["RECORD_KINDS", "Record", "RecordKind", "UnknownFieldPolicy", "parse_record", "select_model"]

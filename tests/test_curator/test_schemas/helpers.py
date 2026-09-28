"""Helpers shared by the schema tests."""

import pytest

from curator.errors import RecordValidationError
from curator.schemas import RecordKind
from curator.schemas import UnknownFieldPolicy
from curator.schemas import parse_record


def msg(role: str, content: str = "text") -> dict[str, str]:
    return {"role": role, "content": content}


def issue_codes(
    raw: object, kind: RecordKind, *, unknown_fields: UnknownFieldPolicy = "reject"
) -> list[tuple[str, str]]:
    """Parse a record that must be invalid and return its (code, field) pairs in report order."""
    with pytest.raises(RecordValidationError) as excinfo:
        parse_record(raw, kind, unknown_fields=unknown_fields)
    return [(issue.code, issue.field) for issue in excinfo.value.issues]

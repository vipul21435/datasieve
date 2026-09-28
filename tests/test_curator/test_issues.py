"""RecordIssue conversion from pydantic errors."""

from typing import Literal

import pytest
from pydantic import BaseModel
from pydantic import ValidationError

from curator.schemas.issues import RecordIssue
from curator.schemas.issues import format_loc
from curator.schemas.issues import issues_from_validation_error


class _Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class _Chat(BaseModel):
    messages: list[_Turn]


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        ((), ""),
        (("prompt",), "prompt"),
        (("messages", 0), "messages[0]"),
        (("messages", 3, "content"), "messages[3].content"),
        (("metadata", "source", "name"), "metadata.source.name"),
    ],
)
def test_format_loc(loc: tuple[int | str, ...], expected: str) -> None:
    assert format_loc(loc) == expected


def test_issues_keep_every_error_with_code_and_path() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _Chat.model_validate({"messages": [{"role": "bot", "content": "hi"}, {"role": "user"}]})

    issues = issues_from_validation_error(excinfo.value)

    assert [(issue.code, issue.field) for issue in issues] == [
        ("literal_error", "messages[0].role"),
        ("missing", "messages[1].content"),
    ]
    assert all(issue.message for issue in issues)


def test_issue_round_trips_to_dict() -> None:
    issue = RecordIssue("blank_text", "prompt", "must not be blank")
    assert issue.to_dict() == {"code": "blank_text", "field": "prompt", "message": "must not be blank"}

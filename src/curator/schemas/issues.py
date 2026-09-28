"""Machine-readable descriptions of why a record was rejected.

A :class:`RecordIssue` is what ends up in the quarantine file, so its ``code``
values are part of curator's public contract: dashboards and dataset cards
group rejected records by code. Codes are either pydantic's own error types
(``missing``, ``extra_forbidden``, ``string_type``, ...) or curator's custom
ones raised by the record schemas (``blank_text``, ``last_turn_not_assistant``,
...) and by the validator stage (``invalid_json``, ``duplicate_id``, ...).
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass

from pydantic import ValidationError
from pydantic_core import ErrorDetails


@dataclass(frozen=True, slots=True)
class RecordIssue:
    """One problem with one record.

    ``field`` is a path such as ``messages[2].content``; the empty string means
    the problem concerns the record as a whole.
    """

    code: str
    field: str
    message: str

    def describe(self) -> str:
        """Render the issue on one line.

        >>> RecordIssue("blank_text", "messages[1].content", "must not be blank").describe()
        'messages[1].content: must not be blank [blank_text]'
        >>> RecordIssue("invalid_json", "", "not JSON").describe()
        '<record>: not JSON [invalid_json]'
        """
        return f"{self.field or '<record>'}: {self.message} [{self.code}]"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def format_loc(loc: tuple[int | str, ...]) -> str:
    """Turn a pydantic error location into a readable field path.

    >>> format_loc(("messages", 2, "content"))
    'messages[2].content'
    >>> format_loc(())
    ''
    """
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else part)
    return path


def issue_from_error(error: ErrorDetails) -> RecordIssue:
    return RecordIssue(code=error["type"], field=format_loc(error["loc"]), message=error["msg"])


def issues_from_validation_error(exc: ValidationError) -> tuple[RecordIssue, ...]:
    """Convert every error in a pydantic ``ValidationError`` to a :class:`RecordIssue`."""
    return tuple(issue_from_error(error) for error in exc.errors(include_url=False, include_input=False))


__all__ = ["RecordIssue", "format_loc", "issue_from_error", "issues_from_validation_error"]

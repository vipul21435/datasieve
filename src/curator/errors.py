"""Error hierarchy for the ``curator`` package.

Every error that curator raises on purpose derives from :class:`CuratorError`, so
callers (the CLI, the HTTP API, tests) can catch one type and still tell the
failure classes apart:

.. code-block:: text

    CuratorError
    +-- ConfigError                 bad settings or pipeline spec   (exit 78)
    |   +-- SettingsError           a CURATOR_* variable is invalid
    |   +-- SpecError               the pipeline spec file is unusable
    +-- DataError                   the data itself is unusable     (exit 65)
        +-- InputFileError          an input file is missing/unreadable (exit 66)
        +-- RecordValidationError   one record fails its schema
        +-- QuarantineThresholdError too many records were quarantined
        +-- StageInputError         a stage's input file holds an unexpected line
        +-- LedgerError             the run ledger file is not a usable SQLite database

Each class carries a stable machine-readable ``code`` and a process
``exit_code`` (1 for the base class, otherwise taken from BSD ``sysexits.h``)
so shell pipelines can branch on the failure class. :meth:`CuratorError.to_dict` gives the JSON shape used for
API error bodies and structured logs.

This module must stay dependency-free: it is imported by everything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar

if TYPE_CHECKING:
    from curator.schemas.issues import RecordIssue

# Process exit codes. 1 is the generic failure; the rest come from BSD sysexits.h.
EX_FAILURE = 1
EX_DATAERR = 65
EX_NOINPUT = 66
EX_CONFIG = 78


class CuratorError(Exception):
    """Base class for every error curator raises deliberately."""

    code: ClassVar[str] = "curator_error"
    exit_code: ClassVar[int] = EX_FAILURE

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable description of the error.

        >>> CuratorError("boom", details={"line": 3}).to_dict()
        {'error': 'curator_error', 'message': 'boom', 'details': {'line': 3}}
        """
        return {"error": self.code, "message": self.message, "details": dict(self.details)}


class ConfigError(CuratorError):
    """The runtime settings or the pipeline spec are invalid."""

    code = "config_error"
    exit_code = EX_CONFIG


class SettingsError(ConfigError):
    """One or more ``CURATOR_*`` settings (environment or ``.env``) are invalid."""

    code = "settings_error"

    def __init__(self, message: str, *, problems: Sequence[str] = ()) -> None:
        super().__init__(message, details={"problems": list(problems)})
        self.problems: tuple[str, ...] = tuple(problems)

    def __str__(self) -> str:
        return "\n  ".join([self.message, *self.problems])


class SpecError(ConfigError):
    """A pipeline spec file is missing, unparseable or fails validation."""

    code = "spec_error"

    def __init__(self, message: str, *, path: Path | None = None, problems: Sequence[str] = ()) -> None:
        details: dict[str, object] = {"problems": list(problems)}
        if path is not None:
            details["path"] = str(path)
        super().__init__(message, details=details)
        self.path = path
        self.problems: tuple[str, ...] = tuple(problems)

    def __str__(self) -> str:
        return "\n  ".join([self.message, *self.problems])


class DataError(CuratorError):
    """Input data cannot be used as-is."""

    code = "data_error"
    exit_code = EX_DATAERR


class InputFileError(DataError):
    """An input file does not exist or cannot be read."""

    code = "input_file_error"
    exit_code = EX_NOINPUT

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message, details={"path": str(path)})
        self.path = path


class RecordValidationError(DataError):
    """A single record does not match its schema.

    ``issues`` lists every problem found, each with a stable ``code``, the
    dotted ``field`` path it applies to and a human-readable ``message``.
    """

    code = "record_invalid"

    def __init__(self, issues: Sequence[RecordIssue]) -> None:
        self.issues: tuple[RecordIssue, ...] = tuple(issues)
        summary = "; ".join(issue.describe() for issue in self.issues) or "record is invalid"
        super().__init__(summary, details={"issues": [issue.to_dict() for issue in self.issues]})


class QuarantineThresholdError(DataError):
    """More records were quarantined than the stage's ``max_invalid_fraction`` allows."""

    code = "quarantine_threshold_exceeded"

    def __init__(self, *, stage: str, invalid: int, total: int, max_fraction: float, quarantine_path: Path) -> None:
        fraction = invalid / total if total else 0.0
        message = (
            f"{stage}: {invalid}/{total} records ({fraction:.1%}) were quarantined, "
            f"above the limit of {max_fraction:.1%}; see {quarantine_path}"
        )
        super().__init__(
            message,
            details={
                "stage": stage,
                "invalid": invalid,
                "total": total,
                "invalid_fraction": fraction,
                "max_invalid_fraction": max_fraction,
                "quarantine_path": str(quarantine_path),
            },
        )
        self.stage = stage
        self.invalid = invalid
        self.total = total
        self.max_fraction = max_fraction
        self.quarantine_path = quarantine_path


class StageInputError(DataError):
    """A stage's input file holds a line that is not what the stage expects.

    Stages after ``validate`` expect one valid record per line, so this usually
    means a stage was pointed at raw data instead of the validated file. The
    ``dedup`` stage also raises it for a reference-set line that is neither a
    record nor an object with the text fields as strings.
    """

    code = "stage_input_invalid"

    def __init__(self, message: str, *, stage: str, line: int, path: Path | None = None) -> None:
        details: dict[str, object] = {"stage": stage, "line": line}
        if path is not None:
            details["path"] = str(path)
        super().__init__(message, details=details)
        self.stage = stage
        self.line = line
        self.path = path


class LedgerError(DataError):
    """The run ledger (see :mod:`curator.ledger`) exists but cannot be read or written as a SQLite database."""

    code = "ledger_error"

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message, details={"path": str(path)})
        self.path = path


__all__ = [
    "EX_CONFIG",
    "EX_DATAERR",
    "EX_FAILURE",
    "EX_NOINPUT",
    "ConfigError",
    "CuratorError",
    "DataError",
    "InputFileError",
    "LedgerError",
    "QuarantineThresholdError",
    "RecordValidationError",
    "SettingsError",
    "SpecError",
    "StageInputError",
]

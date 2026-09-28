"""JSON Lines helpers shared by the pipeline stages.

* :func:`read_lines` streams a file as ``(line number, bytes)`` pairs, so a
  stage can report undecodable lines instead of crashing on them.
* :func:`loads_strict` is ``json.loads`` minus two silent footguns: duplicate
  object keys (the last one would win) and non-finite numbers, whether
  spelled as the non-standard ``NaN`` / ``Infinity`` constants or as a
  literal such as ``1e400`` that overflows a float.
* :func:`encode_json_line` never writes ``NaN`` / ``Infinity``, so every line
  it produces is standard JSON that :func:`loads_strict` reads back.
* :class:`AtomicFile` writes to a temporary file next to the target and only
  replaces the target on :meth:`~AtomicFile.commit`, so readers never see a
  half-written output and a failed run leaves no partial file behind.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Generator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO
from typing import Literal
from typing import NoReturn
from typing import Self

from curator.errors import InputFileError

UTF8_BOM = b"\xef\xbb\xbf"


class StrictJSONError(ValueError):
    """Valid for Python's ``json`` module, but rejected by :func:`loads_strict`."""

    def __init__(self, code: Literal["duplicate_key", "non_finite_number"], message: str) -> None:
        super().__init__(message)
        self.code = code


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJSONError("duplicate_key", f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> NoReturn:
    raise StrictJSONError("non_finite_number", f"{name} is not valid JSON")


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):
        shown = literal if len(literal) <= 32 else f"{literal[:29]}..."
        raise StrictJSONError("non_finite_number", f"number {shown} is out of range for a 64-bit float")
    return value


def loads_strict(text: str) -> object:
    """Decode ``text`` as standard JSON.

    Raises ``json.JSONDecodeError`` for malformed JSON and
    :class:`StrictJSONError` for duplicate keys or non-finite numbers
    (``NaN``, ``Infinity``, or a literal like ``1e400`` that would decode to
    infinity).

    >>> loads_strict('{"a": [1, 2.5, null]}')
    {'a': [1, 2.5, None]}
    >>> loads_strict('{"a": 1, "a": 2}')
    Traceback (most recent call last):
    ...
    curator.io.jsonl.StrictJSONError: duplicate key 'a'
    """
    return json.loads(
        text,
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def encode_json_line(obj: object) -> bytes:
    """Serialise ``obj`` as one UTF-8 JSON line (non-ASCII text kept readable).

    Raises ``UnicodeEncodeError`` if a string holds a lone surrogate, which
    has no UTF-8 encoding, and ``ValueError`` for a NaN or infinite float,
    which has no standard JSON spelling.
    """
    return (json.dumps(obj, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def encode_json_line_lossless(obj: object) -> bytes:
    """Like :func:`encode_json_line`, but writes lone surrogates as ``\\uXXXX`` escapes instead of failing."""
    try:
        return encode_json_line(obj)
    except UnicodeEncodeError:
        return (json.dumps(obj, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def read_lines(path: Path) -> Generator[tuple[int, bytes]]:
    """Yield ``(line_number, content)`` for every line of ``path``; numbers start at 1.

    ``content`` excludes the line ending, and a UTF-8 byte-order mark at the
    start of the file is dropped. The file is opened immediately, so a missing
    or unreadable input raises :class:`~curator.errors.InputFileError` here
    rather than on first iteration. Close the generator (or exhaust it) to
    release the file handle.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise InputFileError(f"input file not found: {path}", path=path) from None
    except OSError as exc:
        raise InputFileError(f"cannot read input file {path}: {exc.strerror or exc}", path=path) from exc
    return _lines(handle)


def _lines(handle: BinaryIO) -> Generator[tuple[int, bytes]]:
    with handle:
        for number, line in enumerate(handle, start=1):
            content = line.rstrip(b"\r\n")
            if number == 1 and content.startswith(UTF8_BOM):
                content = content[len(UTF8_BOM) :]
            yield number, content


class AtomicFile:
    """A binary file that appears at ``path`` only when :meth:`commit` is called.

    Leaving the ``with`` block without committing (including through an
    exception) discards everything written, and an existing file at ``path``
    stays untouched.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        self._handle: BinaryIO = self._tmp_path.open("wb")
        self._closed = False

    def write(self, data: bytes) -> None:
        self._handle.write(data)

    def commit(self) -> None:
        """Flush to disk and atomically move the file into place."""
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._closed = True
        os.replace(self._tmp_path, self.path)

    def discard(self) -> None:
        """Drop everything written so far; no-op after :meth:`commit`."""
        if not self._closed:
            self._handle.close()
            self._closed = True
            self._tmp_path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.discard()


__all__ = [
    "UTF8_BOM",
    "AtomicFile",
    "StrictJSONError",
    "encode_json_line",
    "encode_json_line_lossless",
    "loads_strict",
    "read_lines",
]

"""JSONL helpers: line reading, strict JSON decoding, encoding and atomic writes."""

import json
from pathlib import Path

import pytest

from curator.errors import InputFileError
from curator.io.jsonl import AtomicFile
from curator.io.jsonl import StrictJSONError
from curator.io.jsonl import encode_json_line
from curator.io.jsonl import encode_json_line_lossless
from curator.io.jsonl import loads_strict
from curator.io.jsonl import read_lines


def test_read_lines_numbers_lines_and_strips_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "in.jsonl"
    path.write_bytes(b'{"a": 1}\r\n\n  \n{"b": 2}')

    assert list(read_lines(path)) == [(1, b'{"a": 1}'), (2, b""), (3, b"  "), (4, b'{"b": 2}')]


def test_read_lines_drops_a_leading_byte_order_mark_only(tmp_path: Path) -> None:
    path = tmp_path / "bom.jsonl"
    path.write_bytes(b'\xef\xbb\xbf{"a": 1}\n\xef\xbb\xbf{"b": 2}\n')

    assert list(read_lines(path)) == [(1, b'{"a": 1}'), (2, b'\xef\xbb\xbf{"b": 2}')]


def test_read_lines_fails_eagerly_for_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="input file not found") as excinfo:
        read_lines(tmp_path / "absent.jsonl")
    assert excinfo.value.exit_code == 66


def test_read_lines_rejects_a_directory(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="cannot read input file"):
        read_lines(tmp_path)


def test_loads_strict_accepts_standard_json() -> None:
    assert loads_strict('{"a": {"b": [1, -2.5e3, true, null, "\\u00e9"]}}') == {
        "a": {"b": [1, -2500.0, True, None, "é"]}
    }


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ('{"a": 1, "a": 2}', "duplicate_key"),
        ('{"outer": {"k": 1, "k": 1}}', "duplicate_key"),
        ('{"score": NaN}', "non_finite_number"),
        ('{"score": Infinity}', "non_finite_number"),
        ('{"score": -Infinity}', "non_finite_number"),
    ],
)
def test_loads_strict_rejects_silent_footguns(text: str, code: str) -> None:
    with pytest.raises(StrictJSONError) as excinfo:
        loads_strict(text)
    assert excinfo.value.code == code


def test_loads_strict_raises_decode_errors_for_malformed_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        loads_strict('{"a": ')


def test_encode_json_line_keeps_non_ascii_text_readable() -> None:
    line = encode_json_line({"text": "café 你好"})
    assert line == '{"text": "café 你好"}\n'.encode()
    assert json.loads(line) == {"text": "café 你好"}


def test_lone_surrogates_fail_strict_encoding_but_not_lossless_encoding() -> None:
    obj = {"text": "bad \ud800 surrogate"}
    with pytest.raises(UnicodeEncodeError):
        encode_json_line(obj)
    line = encode_json_line_lossless(obj)
    assert line == b'{"text": "bad \\ud800 surrogate"}\n'
    assert json.loads(line) == obj


def test_atomic_file_appears_only_on_commit(tmp_path: Path) -> None:
    target = tmp_path / "out.jsonl"
    with AtomicFile(target) as out:
        out.write(b"line\n")
        assert not target.exists()
        out.commit()
    assert target.read_bytes() == b"line\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.jsonl"]


def test_atomic_file_without_commit_leaves_existing_target_untouched(tmp_path: Path) -> None:
    target = tmp_path / "out.jsonl"
    target.write_bytes(b"previous run\n")

    with AtomicFile(target) as out:
        out.write(b"partial")

    assert target.read_bytes() == b"previous run\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.jsonl"]


def test_atomic_file_discards_on_exception(tmp_path: Path) -> None:
    target = tmp_path / "out.jsonl"
    with pytest.raises(RuntimeError), AtomicFile(target) as out:
        out.write(b"partial")
        raise RuntimeError
    assert list(tmp_path.iterdir()) == []

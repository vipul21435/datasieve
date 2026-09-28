"""Structured logging: JSON/text formatters, context binding and handler setup."""

import io
import json
import logging
import threading
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from curator.log import BASE_KEYS
from curator.log import LOGGER_NAME
from curator.log import configure_logging
from curator.log import current_context
from curator.log import log_context


@pytest.fixture(autouse=True)
def _restore_curator_logger() -> Iterator[None]:
    """configure_logging mutates a process-wide logger; put it back for the other tests."""
    logger = logging.getLogger(LOGGER_NAME)
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    logger.handlers[:] = saved[0]
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


def _json_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_has_base_keys_first_then_extra_fields(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream=stream)

    logging.getLogger("curator.stages.validate").info("validate.finished", extra={"valid": 98, "invalid": 2})

    [line] = _json_lines(stream)
    assert list(line)[: len(BASE_KEYS)] == list(BASE_KEYS)
    assert line["level"] == "info"
    assert line["logger"] == "curator.stages.validate"
    assert line["event"] == "validate.finished"
    assert line["valid"] == 98
    assert line["invalid"] == 2
    assert str(line["ts"]).endswith("Z")
    assert datetime.fromisoformat(str(line["ts"])).utcoffset() == timedelta(0)


def test_every_line_is_standalone_json(stream: io.StringIO) -> None:
    configure_logging("DEBUG", "json", stream=stream)
    log = logging.getLogger("curator.test")

    log.debug("multi\nline event", extra={"note": 'quote " and newline \n'})
    log.warning("second")

    lines = _json_lines(stream)
    assert [line["event"] for line in lines] == ["multi\nline event", "second"]


def test_level_filters_records(stream: io.StringIO) -> None:
    configure_logging("WARNING", "json", stream=stream)
    log = logging.getLogger("curator.test")

    log.info("hidden")
    log.error("shown")

    assert [line["event"] for line in _json_lines(stream)] == ["shown"]


def test_log_context_binds_fields_and_nests(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream=stream)
    log = logging.getLogger("curator.test")

    with log_context(run_id="r1", stage="validate"):
        log.info("outer")
        with log_context(stage="dedup", shard=3):
            log.info("inner")
        log.info("outer again")
    log.info("outside")

    lines = _json_lines(stream)
    assert lines[0]["run_id"] == "r1"
    assert lines[0]["stage"] == "validate"
    assert (lines[1]["stage"], lines[1]["shard"]) == ("dedup", 3)
    assert lines[2]["stage"] == "validate"
    assert "shard" not in lines[2]
    assert "run_id" not in lines[3]
    assert current_context() == {}


def test_extra_fields_override_context_but_never_base_keys(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream=stream)

    with log_context(stage="ctx", level="ctx-level"):
        logging.getLogger("curator.test").info("evt", extra={"stage": "extra", "logger": "spoofed"})

    [line] = _json_lines(stream)
    assert line["stage"] == "extra"
    assert line["level"] == "info"
    assert line["logger"] == "curator.test"


def test_log_context_is_isolated_per_thread(stream: io.StringIO) -> None:
    seen: dict[str, object] = {}

    def worker() -> None:
        seen.update(current_context())

    with log_context(run_id="main-only"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    assert seen == {}


def test_non_json_values_are_serialised(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream=stream)
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    logging.getLogger("curator.test").info(
        "types", extra={"path": Path("a/b.jsonl"), "when": when, "codes": {"b", "a"}, "obj": object}
    )

    [line] = _json_lines(stream)
    assert line["path"] == "a/b.jsonl"
    assert line["when"] == "2026-01-02T03:04:05+00:00"
    assert line["codes"] == ["a", "b"]
    assert line["obj"] == repr(object)


def test_exception_info_is_structured(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream=stream)

    try:
        _ = 1 / 0
    except ZeroDivisionError:
        logging.getLogger("curator.test").exception("stage.failed")

    [line] = _json_lines(stream)
    assert line["level"] == "error"
    assert line["exc_type"] == "ZeroDivisionError"
    assert line["exc_message"] == "division by zero"
    assert "Traceback" in str(line["traceback"])


def test_reconfiguring_replaces_the_handler_and_keeps_foreign_ones(stream: io.StringIO) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    foreign = logging.NullHandler()
    logger.addHandler(foreign)
    first = io.StringIO()

    configure_logging("INFO", "json", stream=first)
    configure_logging("INFO", "text", stream=stream)
    logging.getLogger("curator.test").info("once")

    assert first.getvalue() == ""
    assert stream.getvalue().count("once") == 1
    assert foreign in logger.handlers
    assert sum(isinstance(h, logging.StreamHandler) for h in logger.handlers) == 1


def test_records_do_not_reach_the_root_logger(stream: io.StringIO) -> None:
    root = logging.getLogger()
    root_stream = io.StringIO()
    root_handler = logging.StreamHandler(root_stream)
    root.addHandler(root_handler)
    try:
        configure_logging("INFO", "json", stream=stream)
        logging.getLogger("curator.test").info("only-once")
    finally:
        root.removeHandler(root_handler)

    assert root_stream.getvalue() == ""
    assert "only-once" in stream.getvalue()


def test_text_format_appends_fields_as_key_value(stream: io.StringIO) -> None:
    configure_logging("INFO", "text", stream=stream)

    with log_context(run_id="r1"):
        logging.getLogger("curator.test").info("validate.finished", extra={"valid": 2, "path": Path("x.jsonl")})

    line = stream.getvalue().strip()
    assert "INFO" in line
    assert "curator.test: validate.finished" in line
    assert line.endswith('run_id="r1" valid=2 path="x.jsonl"')

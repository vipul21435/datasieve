"""Fixtures shared by the curator tests."""

import io
import json
import logging
from collections.abc import Callable
from collections.abc import Iterator

import pytest

from curator.log import LOGGER_NAME
from curator.log import configure_logging

JsonLogLines = Callable[[], list[dict[str, object]]]


@pytest.fixture
def json_log() -> Iterator[JsonLogLines]:
    """Route curator's logs to an in-memory JSON stream; call the fixture value to get the parsed lines.

    The process-wide ``curator`` logger is restored afterwards, so pytest's
    ``caplog`` keeps working in later tests.
    """
    logger = logging.getLogger(LOGGER_NAME)
    saved = (list(logger.handlers), logger.level, logger.propagate)
    stream = io.StringIO()
    configure_logging("DEBUG", "json", stream=stream)
    yield lambda: [json.loads(text) for text in stream.getvalue().splitlines()]
    logger.handlers[:] = saved[0]
    logger.setLevel(saved[1])
    logger.propagate = saved[2]

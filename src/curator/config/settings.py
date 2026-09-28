"""Process-level settings, read from ``CURATOR_*`` environment variables.

Settings describe *where and how* curator runs (logging, working directory),
not *what* a pipeline does; that lives in the pipeline spec file (see
:mod:`curator.config.spec`). Precedence, highest first:

1. keyword arguments passed to :func:`load_settings` (for example CLI flags);
2. ``CURATOR_*`` environment variables;
3. a ``.env`` file in the current directory (see ``.env.example``);
4. the defaults below.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import ValidationError
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from curator.errors import SettingsError
from curator.log import LogFormat
from curator.log import LogLevel

ENV_PREFIX: Final = "CURATOR_"

logger = logging.getLogger(__name__)


class CuratorSettings(BaseSettings):
    """Runtime settings. Every field maps to ``CURATOR_<FIELD>``, e.g. ``CURATOR_LOG_LEVEL``."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        # A .env file may hold variables for other tools; they are not ours to reject.
        extra="ignore",
        frozen=True,
        validate_default=True,
    )

    log_level: LogLevel = "INFO"
    """Minimum level for curator's logs."""

    log_format: LogFormat = "json"
    """``json`` for one JSON object per line (machines), ``text`` for humans."""

    work_dir: Path = Path(".curator")
    """Root for run outputs; a pipeline without ``output.dir`` writes to ``<work_dir>/<pipeline name>``."""

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_case_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("log_format", mode="before")
    @classmethod
    def _lower_case_format(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


def env_var_name(field: str) -> str:
    """The environment variable that sets ``field``.

    >>> env_var_name("log_level")
    'CURATOR_LOG_LEVEL'
    """
    return f"{ENV_PREFIX}{field.upper()}"


def unknown_env_vars(environ: Mapping[str, str]) -> list[str]:
    """``CURATOR_*`` variables in ``environ`` that match no setting (usually typos).

    >>> unknown_env_vars({"CURATOR_LOG_LEVEL": "info", "CURATOR_LOGLEVEL": "debug", "HOME": "/"})
    ['CURATOR_LOGLEVEL']
    """
    known = {env_var_name(name) for name in CuratorSettings.model_fields}
    return sorted(key for key in environ if key.upper().startswith(ENV_PREFIX) and key.upper() not in known)


def load_settings(**overrides: Any) -> CuratorSettings:
    """Build settings from the environment, ``.env`` and ``overrides``.

    Raises :class:`~curator.errors.SettingsError` naming each bad variable,
    instead of pydantic's ``ValidationError``. Pass ``_env_file=None`` to skip
    the ``.env`` file, or ``_env_file=path`` to read a different one.
    """
    for name in unknown_env_vars(os.environ):
        logger.warning("settings.unknown_env_var", extra={"variable": name})
    try:
        return CuratorSettings(**overrides)
    except ValidationError as exc:
        problems = [
            f"{env_var_name(str(error['loc'][0])) if error['loc'] else ENV_PREFIX + '*'}: {error['msg']}"
            for error in exc.errors(include_url=False)
        ]
        raise SettingsError("invalid curator settings", problems=problems) from None


__all__ = ["ENV_PREFIX", "CuratorSettings", "env_var_name", "load_settings", "unknown_env_vars"]

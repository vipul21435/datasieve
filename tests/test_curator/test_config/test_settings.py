"""CuratorSettings: CURATOR_* environment variables, .env files and error reporting."""

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from curator.config import CuratorSettings
from curator.config import load_settings
from curator.config.settings import ENV_PREFIX
from curator.config.settings import env_var_name
from curator.config.settings import unknown_env_vars
from curator.errors import SettingsError

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """No CURATOR_* variables from the developer's shell, and no stray .env in the working directory."""
    for key in list(os.environ):
        if key.upper().startswith(ENV_PREFIX):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_defaults() -> None:
    settings = load_settings()
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.work_dir == Path(".curator")


def test_environment_variables_use_the_curator_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURATOR_LOG_LEVEL", "debug")
    monkeypatch.setenv("CURATOR_LOG_FORMAT", " TEXT ")
    monkeypatch.setenv("CURATOR_WORK_DIR", "/data/curator")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")  # unprefixed: not ours

    settings = load_settings()

    assert settings.log_level == "DEBUG"
    assert settings.log_format == "text"
    assert settings.work_dir == Path("/data/curator")


def test_dotenv_file_is_read_and_environment_wins(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (isolated_env / ".env").write_text(
        "CURATOR_LOG_LEVEL=WARNING\nCURATOR_WORK_DIR=from-dotenv\nOTHER_TOOL_TOKEN=ignored\n", encoding="utf-8"
    )
    monkeypatch.setenv("CURATOR_WORK_DIR", "from-env")

    settings = load_settings()

    assert settings.log_level == "WARNING"
    assert settings.work_dir == Path("from-env")


def test_keyword_overrides_win_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURATOR_LOG_LEVEL", "ERROR")
    assert load_settings(log_level="DEBUG").log_level == "DEBUG"


def test_explicit_env_file_and_opting_out(isolated_env: Path) -> None:
    (isolated_env / ".env").write_text("CURATOR_LOG_LEVEL=ERROR\n", encoding="utf-8")
    other = isolated_env / "ci.env"
    other.write_text("CURATOR_LOG_LEVEL=CRITICAL\n", encoding="utf-8")

    assert load_settings(_env_file=other).log_level == "CRITICAL"
    assert load_settings(_env_file=None).log_level == "INFO"


@pytest.mark.parametrize(
    ("variable", "value"),
    [("CURATOR_LOG_LEVEL", "LOUD"), ("CURATOR_LOG_FORMAT", "yaml")],
)
def test_invalid_values_raise_settings_error_naming_the_variable(
    variable: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(SettingsError) as excinfo:
        load_settings()

    [problem] = excinfo.value.problems
    assert problem.startswith(f"{variable}: ")
    assert variable in str(excinfo.value)
    assert excinfo.value.exit_code == 78


def test_unknown_curator_variables_are_reported_as_likely_typos(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CURATOR_LOGLEVEL", "DEBUG")

    with caplog.at_level(logging.WARNING, logger="curator"):
        settings = load_settings()

    assert settings.log_level == "INFO"
    [record] = [r for r in caplog.records if r.getMessage() == "settings.unknown_env_var"]
    assert record.__dict__["variable"] == "CURATOR_LOGLEVEL"


def test_unknown_env_vars_ignores_other_prefixes() -> None:
    assert unknown_env_vars({"CURATOR_WORK_DIR": "x", "CURATORS": "y", "PATH": "/bin"}) == []


def test_settings_are_immutable() -> None:
    settings = load_settings()
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"


def test_env_example_documents_exactly_the_settings_and_loads_cleanly() -> None:
    example = REPO_ROOT / ".env.example"
    keys = [
        line.split("=", 1)[0]
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert sorted(keys) == sorted(env_var_name(name) for name in CuratorSettings.model_fields)
    assert load_settings(_env_file=example) == load_settings(_env_file=None)

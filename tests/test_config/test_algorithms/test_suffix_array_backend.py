"""The optional Rust suffix-array backend must fail fast with an actionable error."""

from pathlib import Path

import pytest

from text_dedup import suffix_array
from text_dedup.config.algorithms import suffix_array as sa_config
from text_dedup.config.algorithms.suffix_array import BACKEND_REQUIRED_FILES
from text_dedup.config.algorithms.suffix_array import SuffixArrayAlgorithmConfig
from text_dedup.config.algorithms.suffix_array import SuffixArrayBackendError
from text_dedup.config.base import load_config_from_toml


def _fake_backend(root: Path) -> Path:
    for name in BACKEND_REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    return root


def _config(repo: Path) -> SuffixArrayAlgorithmConfig:
    return SuffixArrayAlgorithmConfig(algorithm_name="suffix_array", text_column="text", google_repo_path=str(repo))


def test_missing_submodule_points_at_git_submodule_init(tmp_path: Path) -> None:
    with pytest.raises(SuffixArrayBackendError, match="git submodule update --init") as excinfo:
        _config(tmp_path / "absent").check_backend()
    for name in BACKEND_REQUIRED_FILES:
        assert name in str(excinfo.value)


def test_partial_checkout_lists_only_missing_files(tmp_path: Path) -> None:
    repo = _fake_backend(tmp_path)
    (repo / "Cargo.toml").unlink()
    with pytest.raises(SuffixArrayBackendError, match=r"missing: Cargo\.toml\)"):
        _config(repo).check_backend()


def test_missing_cargo_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sa_config.shutil, "which", lambda _cmd: None)
    with pytest.raises(SuffixArrayBackendError, match="cargo"):
        _config(_fake_backend(tmp_path)).check_backend()


def test_ready_backend_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sa_config.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    _config(_fake_backend(tmp_path)).check_backend()


def test_main_checks_backend_before_touching_the_filesystem(tmp_path: Path) -> None:
    repo = tmp_path / "deduplicate-text-datasets"
    toml = tmp_path / "config.toml"
    toml.write_text(
        f"""
[input]
input_type = "local_files"
file_type = "parquet"
read_arguments = {{ path = "{tmp_path / "data"}" }}

[algorithm]
algorithm_name = "suffix_array"
text_column = "text"
google_repo_path = "{repo}"

[output]
output_dir = "{tmp_path / "output"}"

[debug]
enable_profiling = false
"""
    )
    config = load_config_from_toml(toml)

    with pytest.raises(SuffixArrayBackendError):
        suffix_array.main(config)
    assert not repo.exists()

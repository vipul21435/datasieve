"""Packaging and module-boundary checks for the fork's ``curator`` package."""

import subprocess
import sys
from importlib.metadata import distribution
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path

import pytest

import curator

# Heavy third-party modules that must not load as a side effect of `import curator`.
HEAVY_MODULES = ("datasets", "polars", "numpy", "scipy", "pyarrow", "text_dedup")


def test_version_matches_installed_distribution() -> None:
    assert curator.DISTRIBUTION_NAME == "sft-data-curator"
    assert curator.__version__ == version(curator.DISTRIBUTION_NAME)


def test_package_is_typed() -> None:
    spec = find_spec("curator")
    assert spec is not None
    assert spec.origin is not None
    assert (Path(spec.origin).parent / "py.typed").is_file()


@pytest.mark.parametrize("package", ["curator", "text_dedup"])
def test_both_packages_resolve_from_the_same_src_tree(package: str) -> None:
    spec = find_spec(package)
    assert spec is not None
    assert spec.origin is not None
    assert Path(spec.origin).parent.parent.name == "src"


def test_upstream_text_dedup_still_importable() -> None:
    from text_dedup.utils.jaccard import jaccard_similarity

    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0


def test_distribution_metadata_credits_upstream() -> None:
    meta = distribution(curator.DISTRIBUTION_NAME).metadata
    urls = meta.get_all("Project-URL") or []
    assert any("ChenghaoMou/text-dedup" in url for url in urls)


def test_import_is_lightweight() -> None:
    """`import curator` in a fresh interpreter must not drag in heavy dependencies."""
    probe = f"import sys, curator; print(','.join(m for m in {HEAVY_MODULES!r} if m in sys.modules))"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""

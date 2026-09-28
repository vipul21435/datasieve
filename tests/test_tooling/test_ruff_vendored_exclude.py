"""The README's lint commands must stay clean once the optional backend submodule is checked out.

`third_party/deduplicate-text-datasets` is vendored Google code that does not follow this
repo's ruff rules. These tests run ruff with the repo's real ``[tool.ruff]`` config against a
throwaway tree that contains a rule-breaking file under ``third_party/``, which is what a
developer gets after ``git submodule update --init``.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("ruff")

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDORED = Path("third_party/deduplicate-text-datasets/scripts/load_dataset.py")
# Fixable lint errors (E401, F401), an unfixable one (S101) and a formatting change.
VENDORED_SOURCE = "import sys, os\nassert  sys\n"
OWN = Path("src/pkg/module.py")


def _ruff(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "ruff", *args, "--no-cache"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for rel, source in ((VENDORED, VENDORED_SOURCE), (OWN, "VALUE = 1\n")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(source)
    return tmp_path


def test_fixture_file_really_breaks_the_rules(tree: Path) -> None:
    """Guards the other tests: without the exclude, ruff must reject the vendored file."""
    result = _ruff(tree, "check", "--no-fix", "--no-force-exclude", str(VENDORED))
    assert result.returncode == 1, result.stdout + result.stderr


def test_third_party_is_not_discovered(tree: Path) -> None:
    result = _ruff(tree, "check", "--show-files", ".")
    assert result.returncode == 0, result.stderr
    # ruff prints absolute paths; compare relative ones since tmp_path embeds the test name.
    shown = {Path(line).resolve().relative_to(tree.resolve()) for line in result.stdout.splitlines()}
    assert OWN in shown
    assert not [path for path in shown if path.parts[0] == "third_party"]


def test_documented_commands_pass_and_leave_vendored_code_untouched(tree: Path) -> None:
    # Exactly the README form: `ruff check .` picks up `fix = true` from the config.
    check = _ruff(tree, "check", ".")
    assert check.returncode == 0, check.stdout + check.stderr
    fmt = _ruff(tree, "format", "--check", ".")
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    assert (tree / VENDORED).read_text() == VENDORED_SOURCE


def test_explicitly_passed_vendored_file_is_still_excluded(tree: Path) -> None:
    """Editors and hooks pass file paths directly; `force-exclude` must still apply."""
    check = _ruff(tree, "check", str(VENDORED))
    assert check.returncode == 0, check.stdout + check.stderr
    fmt = _ruff(tree, "format", str(VENDORED))
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr
    assert (tree / VENDORED).read_text() == VENDORED_SOURCE

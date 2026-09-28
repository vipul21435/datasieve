"""``python -m curator``: the funnel, the JSON report, ``check`` and the exit codes."""

import io
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from curator.cli import main
from curator.errors import EX_CONFIG
from curator.errors import EX_DATAERR
from curator.log import LOGGER_NAME

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
DEMO_SPEC = EXAMPLES / "sft-demo.yaml"


@pytest.fixture(autouse=True)
def restore_curator_logger() -> Iterator[None]:
    """``main`` configures the process-wide ``curator`` logger; put it back so ``caplog`` keeps working."""
    logger = logging.getLogger(LOGGER_NAME)
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    logger.handlers[:] = saved[0]
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_run_prints_the_stage_funnel(tmp_path: Path) -> None:
    code, out, err = run_cli("run", str(DEMO_SPEC), "--work-dir", str(tmp_path), "--log-level", "WARNING")

    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("pipeline sft-demo-300: ")
    assert lines[0].endswith(str(tmp_path / "sft-demo-300" / "deduped.jsonl"))
    assert "validate" in lines[1] and "299 in ->    287 out" in lines[1] and "12 quarantined" in lines[1]
    assert "dedup" in lines[2] and "287 in ->    220 out" in lines[2]
    assert "contaminated=12, exact_duplicate=30, near_duplicate=25" in lines[2]
    assert lines[3].startswith("finished in ") and lines[3].endswith(" records/s)")
    assert err == ""  # WARNING level: nothing logged for a clean run


def test_run_json_reports_every_stage_and_the_timing(tmp_path: Path) -> None:
    code, out, _ = run_cli("run", str(DEMO_SPEC), "--work-dir", str(tmp_path), "--json", "--log-level", "ERROR")

    assert code == 0
    report = json.loads(out)
    assert report["pipeline"] == "sft-demo-300"
    assert [stage["stage"] for stage in report["stages"]] == ["validate", "dedup"]
    assert report["stages"][1]["reasons"] == {"contaminated": 12, "exact_duplicate": 30, "near_duplicate": 25}
    assert report["elapsed_seconds"] > 0
    assert report["records_per_second"] == pytest.approx(299 / report["elapsed_seconds"], rel=0.01)
    assert Path(report["output"]).read_text(encoding="utf-8").count("\n") == 220


def test_logs_go_to_stderr_in_the_chosen_format(tmp_path: Path) -> None:
    _, out, err = run_cli("run", str(DEMO_SPEC), "--work-dir", str(tmp_path), "--log-format", "json")

    events = [json.loads(line)["event"] for line in err.splitlines()]
    assert events[0] == "pipeline.started" and events[-1] == "pipeline.finished"
    assert out.startswith("pipeline sft-demo-300")


def test_check_lists_the_stages_without_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli("check", str(EXAMPLES / "sft-dedup.yaml"))

    assert code == 0
    assert out.splitlines()[0] == f"sft-dedup-demo: sft records from {EXAMPLES / 'data' / 'sft_dedup_sample.jsonl'}"
    assert out.splitlines()[-2:] == ["  1. validate", "  2. dedup"]
    assert not (tmp_path / ".curator").exists()


def test_bad_spec_exits_with_the_config_code(tmp_path: Path) -> None:
    spec = tmp_path / "bad.yaml"
    spec.write_text("version: 1\nname: x\ninput: {path: a.jsonl, kind: sft}\nstages: [{stage: validate, typo: 1}]\n")

    code, out, err = run_cli("run", str(spec))

    assert (code, out) == (EX_CONFIG, "")
    assert err.startswith("error [spec_error]: invalid pipeline spec") and "typo" in err


def test_missing_spec_exits_with_the_config_code(tmp_path: Path) -> None:
    code, _, err = run_cli("check", str(tmp_path / "nope.yaml"))
    assert code == EX_CONFIG and "pipeline spec not found" in err


def test_quarantine_threshold_exits_with_the_data_code(tmp_path: Path) -> None:
    (tmp_path / "raw.jsonl").write_text('{"prompt": "a", "response": ""}\n{"prompt": "b", "response": "ok"}\n')
    spec = tmp_path / "strict.yaml"
    spec.write_text(
        "version: 1\nname: strict\ninput: {path: raw.jsonl, kind: sft}\n"
        "stages: [{stage: validate, max_invalid_fraction: 0.1}]\n"
    )

    code, _, err = run_cli("run", str(spec), "--work-dir", str(tmp_path / "work"), "--log-level", "ERROR")

    assert code == EX_DATAERR and err.startswith("error [quarantine_threshold_exceeded]")


def test_usage_error_exits_with_2() -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli("run")
    assert exc.value.code == 2


def test_module_entry_point(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - our own interpreter, fixed arguments
        [sys.executable, "-m", "curator", "check", str(DEMO_SPEC)], capture_output=True, text=True, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("sft-demo-300: sft records from ")

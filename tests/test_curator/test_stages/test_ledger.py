"""The ``ledger`` stage and the SQLite ledger behind it: re-runs, later batches, collisions and failures."""

import sqlite3
from pathlib import Path

import pytest

from curator.config.spec import LedgerStageSpec
from curator.config.spec import parse_spec
from curator.errors import ConfigError
from curator.errors import InputFileError
from curator.errors import LedgerError
from curator.errors import StageInputError
from curator.io.jsonl import encode_json_line
from curator.io.jsonl import loads_strict
from curator.ledger import Ledger
from curator.ledger import config_digest
from curator.ledger import new_run
from curator.ledger import summarise_collisions
from curator.ledger import summarise_stats
from curator.pipeline import run_pipeline
from curator.stages.ledger import ledger_path_for
from curator.stages.ledger import run_ledger


def write_records(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(encode_json_line(record) for record in records))
    return path


def record(record_id: str, prompt: str, response: str = "an answer") -> dict[str, object]:
    return {"id": record_id, "prompt": prompt, "response": response}


BATCH_1 = [record("a", "What is 1 + 1?"), record("b", "Name a colour."), record("c", "Name a shape.")]


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [loads_strict(line) for line in path.read_text().splitlines()]  # type: ignore[misc]


def test_first_run_records_every_content_and_keeps_all(tmp_path: Path) -> None:
    source = write_records(tmp_path / "batch-1.jsonl", BATCH_1)

    report = run_ledger(source, tmp_path / "out", kind="sft")

    assert (report.total, report.kept, report.new, report.rerun, report.skipped) == (3, 3, 3, 0, 0)
    assert report.output_path.read_bytes() == source.read_bytes()
    assert report.seen_path.read_bytes() == b""
    assert report.ledger_path == tmp_path / "out" / "ledger.sqlite"
    summary = report.to_dict()
    assert summary["stage"] == "ledger" and summary["reasons"] == {} and summary["dropped"] == 0
    with Ledger(report.ledger_path) as ledger:
        stats = ledger.stats()
    assert (stats["runs"], stats["records"], stats["distinct_ids"], stats["collisions"]) == (1, 3, 3, 0)
    [run] = stats["run_list"]  # type: ignore[misc]
    assert run["run_id"] == report.run_id and run["kept"] == 3 and run["source"] == str(source)


def test_rerun_over_the_same_input_is_byte_identical_and_reports_the_earlier_run(tmp_path: Path) -> None:
    source = write_records(tmp_path / "batch-1.jsonl", BATCH_1)
    first = run_ledger(source, tmp_path / "out", kind="sft")
    first_output = first.output_path.read_bytes()

    second = run_ledger(source, tmp_path / "out", kind="sft")

    assert second.output_path.read_bytes() == first_output
    assert (second.kept, second.new, second.rerun, second.skipped) == (3, 0, 3, 0)
    assert second.earlier_runs == {first.run_id: 3}
    seen = read_jsonl(second.seen_path)
    assert [entry["action"] for entry in seen] == ["kept"] * 3
    assert {entry["seen_run"] for entry in seen} == {first.run_id}
    with Ledger(second.ledger_path) as ledger:
        assert ledger.stats()["records"] == 3  # the ledger does not grow on a re-run
        assert [run.rerun for run in ledger.runs()] == [0, 3]


def test_a_later_batch_skips_content_an_earlier_batch_delivered(tmp_path: Path) -> None:
    ledger_path = tmp_path / "shared.sqlite"
    config = LedgerStageSpec(path=ledger_path)
    first = run_ledger(
        write_records(tmp_path / "batch-1.jsonl", BATCH_1), tmp_path / "run-1", kind="sft", config=config
    )
    batch_2 = [record("d", "Name a planet."), record("x", "name a COLOUR."), record("c", "Name a shape.")]
    source = write_records(tmp_path / "batch-2.jsonl", batch_2)

    second = run_ledger(source, tmp_path / "run-2", kind="sft", config=config)

    assert (second.total, second.kept, second.new, second.rerun, second.skipped) == (3, 1, 1, 0, 2)
    assert read_jsonl(second.output_path) == [batch_2[0]]
    seen = read_jsonl(second.seen_path)
    assert [(entry["line"], entry["id"], entry["action"], entry["seen_id"]) for entry in seen] == [
        (2, "x", "skipped", "b"),
        (3, "c", "skipped", "c"),
    ]
    assert seen[0]["seen_run"] == first.run_id and seen[0]["seen_source"] == str(tmp_path / "batch-1.jsonl")
    assert second.to_dict()["reasons"] == {"seen": 2}
    assert second.to_dict()["dropped_fraction"] == pytest.approx(2 / 3, abs=1e-6)


def test_collisions_report_same_id_and_same_content_across_batches(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite"
    config = LedgerStageSpec(path=ledger_path)
    run_ledger(write_records(tmp_path / "b1.jsonl", BATCH_1), tmp_path / "r1", kind="sft", config=config)
    batch_2 = [record("a", "What is 2 + 2?"), record("z", "Name a colour.")]
    run_ledger(write_records(tmp_path / "b2.jsonl", batch_2), tmp_path / "r2", kind="sft", config=config)

    with Ledger.open_existing(ledger_path) as ledger:
        collisions = ledger.collisions()
        stats = ledger.stats()

    assert [(collision.kind, collision.key) for collision in collisions][:1] == [("same_id", "a")]
    same_id, same_content = collisions
    assert [member.line for member in same_id.members] == [1, 1]
    assert len({member.digest for member in same_id.members}) == 2
    assert same_content.kind == "same_content"
    assert [member.record_id for member in same_content.members] == ["b", "z"]
    assert stats["collisions"] == 2 and stats["records"] == 5 and stats["distinct_contents"] == 4
    text = summarise_collisions(collisions)
    assert text.startswith("same id, different content: 1\n  a: ")
    assert "same content, different ids: 1\n  " in text and "b (run " in text
    assert summarise_stats(stats).startswith(f"ledger {ledger_path}: 2 runs, 5 records, 4 distinct contents")
    assert summarise_collisions([]) == "same id, different content: 0\nsame content, different ids: 0"


def test_content_ignores_case_and_whitespace_but_not_the_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite"
    config = LedgerStageSpec(path=ledger_path, fields=["prompt"])
    run_ledger(write_records(tmp_path / "b1.jsonl", BATCH_1), tmp_path / "r1", kind="sft", config=config)
    later = [record("a", "  what IS 1 + 1? ", "a different answer")]

    report = run_ledger(write_records(tmp_path / "b2.jsonl", later), tmp_path / "r2", kind="sft", config=config)

    assert report.skipped == 1  # only the prompt is hashed, so the changed response does not matter


def test_a_failed_run_leaves_the_ledger_and_outputs_untouched(tmp_path: Path) -> None:
    source = write_records(tmp_path / "b1.jsonl", BATCH_1)
    first = run_ledger(source, tmp_path / "out", kind="sft")
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(source.read_bytes() + b'{"prompt": 1}\n')

    with pytest.raises(StageInputError, match="line 4 of .* is not a valid sft record"):
        run_ledger(bad, tmp_path / "out", kind="sft")

    assert first.output_path.read_bytes() == source.read_bytes()
    with Ledger(first.ledger_path) as ledger:
        assert ledger.stats()["runs"] == 1
    (tmp_path / "x.jsonl").write_bytes(b"{\n")
    with pytest.raises(StageInputError, match="not valid JSON"):
        run_ledger(tmp_path / "x.jsonl", tmp_path / "out", kind="sft")
    (tmp_path / "list.jsonl").write_bytes(b"[1]\n")
    with pytest.raises(StageInputError, match="not a JSON object"):
        run_ledger(tmp_path / "list.jsonl", tmp_path / "out", kind="sft")


def test_errors_for_missing_input_bad_ledger_and_clobbered_input(tmp_path: Path) -> None:
    with pytest.raises(InputFileError):
        run_ledger(tmp_path / "missing.jsonl", tmp_path / "out", kind="sft")
    source = write_records(tmp_path / "b1.jsonl", BATCH_1)
    not_sqlite = tmp_path / "ledger.sqlite"
    not_sqlite.write_text("not a database")
    with pytest.raises(LedgerError, match="is unusable") as info:
        run_ledger(source, tmp_path / "out", kind="sft", config=LedgerStageSpec(path=not_sqlite))
    assert info.value.to_dict()["details"] == {"path": str(not_sqlite)}
    with pytest.raises(InputFileError, match="ledger not found"):
        Ledger.open_existing(tmp_path / "nope.sqlite")
    with pytest.raises(ConfigError, match="would overwrite the input"):
        run_ledger(source, tmp_path, kind="sft", config=LedgerStageSpec(output_file="b1.jsonl"))


def rebuild_table(path: Path, table: str, columns: str) -> None:
    db = sqlite3.connect(path)
    db.execute(f"DROP TABLE {table}")
    db.execute(f"CREATE TABLE {table} ({columns})")
    db.commit()
    db.close()


def test_ledger_reads_fail_loudly_on_a_broken_schema(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as ledger:
        ledger.commit()
    rebuild_table(path, "records", "seq INTEGER PRIMARY KEY, record_id TEXT")  # no digest column
    with Ledger(path) as ledger:
        with pytest.raises(LedgerError, match="no such column: digest"):
            ledger.stats()
        with pytest.raises(LedgerError):
            ledger.collisions()
    rebuild_table(path, "runs", "run_id TEXT PRIMARY KEY")
    with Ledger(path) as ledger, pytest.raises(LedgerError, match="no such column"):
        ledger.runs()
    rebuild_table(path, "records", "only_column TEXT")  # the index cannot even be created
    with pytest.raises(LedgerError, match="is unusable"):
        Ledger(path)


def test_a_ledger_locked_by_another_run_fails_fast_with_ledger_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_records(tmp_path / "b1.jsonl", BATCH_1)
    path = tmp_path / "ledger.sqlite"
    first = run_ledger(source, tmp_path / "one", kind="sft", config=LedgerStageSpec(path=path))
    holder = sqlite3.connect(path)  # a second run that is still writing
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO runs VALUES ('other', 'p', 'c', 's', 'd', 't', 0, 0, 0, 0)")
    try:
        with Ledger(path, timeout=0.05) as ledger, pytest.raises(LedgerError, match="locked by another run") as info:
            ledger.begin_run(new_run("demo", source, {}))
        assert info.value.to_dict()["details"] == {"path": str(path)}
        monkeypatch.setattr("curator.ledger.DEFAULT_LOCK_TIMEOUT", 0.05)
        with pytest.raises(LedgerError, match="locked by another run"):
            run_ledger(source, tmp_path / "two", kind="sft", config=LedgerStageSpec(path=path))
        assert not (tmp_path / "two").exists() or not list((tmp_path / "two").iterdir())
    finally:
        holder.rollback()
        holder.close()
    with Ledger(path) as ledger:  # the failed run left no trace; the lock is gone
        assert [run.run_id for run in ledger.runs()] == [first.run_id]
        assert ledger.stats()["records"] == len(BATCH_1)


def test_run_info_and_digests(tmp_path: Path) -> None:
    source = write_records(tmp_path / "b1.jsonl", BATCH_1)
    run = new_run("demo", source, {"k": 1})
    assert run.run_id.startswith("demo-") and run.pipeline == "demo" and run.started_at.endswith("Z")
    assert run.config_digest == config_digest({"k": 1}) and len(run.source_digest) == 16
    assert new_run("demo", source, {"k": 1}).source_digest == run.source_digest
    with pytest.raises(InputFileError):
        new_run("demo", tmp_path / "missing.jsonl", {})
    assert ledger_path_for(LedgerStageSpec(), work_dir=tmp_path / "w") == tmp_path / "w" / "ledger.sqlite"


def test_pipeline_runs_the_ledger_after_dedup_with_the_work_dir_ledger(tmp_path: Path) -> None:
    source = write_records(tmp_path / "raw.jsonl", [*BATCH_1, record("dup", "what is 1 + 1?")])
    spec = parse_spec({
        "version": 1,
        "name": "ledgered",
        "input": {"path": str(source), "kind": "sft"},
        "stages": [{"stage": "validate"}, {"stage": "dedup"}, {"stage": "ledger"}],
    })

    first = run_pipeline(spec, work_dir=tmp_path / "work")
    second = run_pipeline(spec, work_dir=tmp_path / "work")

    assert [stage.to_dict()["stage"] for stage in first.stages] == ["validate", "dedup", "ledger"]
    assert first.output_path == tmp_path / "work" / "ledgered" / "unseen.jsonl"
    assert first.output_path.read_bytes() == second.output_path.read_bytes()
    assert first.stages[2].to_dict()["new"] == 3 and second.stages[2].to_dict()["rerun"] == 3
    assert (tmp_path / "work" / "ledger.sqlite").is_file()
    with Ledger(tmp_path / "work" / "ledger.sqlite") as ledger:
        runs = ledger.runs()
    assert [run.pipeline for run in runs] == ["ledgered", "ledgered"]
    assert runs[0].config_digest == runs[1].config_digest == config_digest(spec.model_dump(mode="json"))


def test_spec_rejects_bad_ledger_options() -> None:
    base = {"version": 1, "name": "x", "input": {"path": "in.jsonl", "kind": "preference"}}
    with pytest.raises(Exception, match="output_file and seen_file must be different"):
        parse_spec({**base, "stages": [{"stage": "validate"}, {"stage": "ledger", "seen_file": "unseen.jsonl"}]})
    with pytest.raises(Exception, match="fields lists response, but preference records only have"):
        parse_spec({**base, "stages": [{"stage": "validate"}, {"stage": "ledger", "fields": ["response"]}]})
    spec = parse_spec({**base, "stages": [{"stage": "validate"}, {"stage": "ledger", "path": "runs/l.sqlite"}]})
    assert spec.stages[1].fields_for("preference") == ("prompt", "chosen", "rejected")  # type: ignore[union-attr]

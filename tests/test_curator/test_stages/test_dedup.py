"""The dedup stage: exact and near duplicates, contamination against a reference set, files and determinism."""

import json
import logging
from pathlib import Path

import pytest

from curator.config import DedupStageSpec
from curator.config import NearDuplicateSpec
from curator.config import NormalizeSpec
from curator.config import ReferenceSpec
from curator.errors import ConfigError
from curator.errors import InputFileError
from curator.errors import StageInputError
from curator.schemas import RecordKind
from curator.stages.dedup import ContentIndex
from curator.stages.dedup import Dropped
from curator.stages.dedup import Kept
from curator.stages.dedup import Outcome
from curator.stages.dedup import dedup_lines
from curator.stages.dedup import load_reference
from curator.stages.dedup import run_dedup

from ..conftest import JsonLogLines

# Family A: 68 words. One changed word touches three word 3-grams (63 shared of 69: 0.913);
# five changed words leave 0.63. Pairs need a margin above the 0.8 threshold, because banded LSH
# proposes a pair sitting right at the threshold with only even odds.
PROMPT = (
    "Please write a short friendly reminder email to the whole team about the quarterly planning meeting "
    "scheduled for next Monday morning in the main conference room, mention that the agenda and the slides "
    "from last quarter are already shared in the planning folder, and ask everyone to confirm attendance by Friday."
)
RESPONSE = "Hi all, see you on Monday at nine; the room is booked and coffee is on me."
ONE_WORD = PROMPT.replace("friendly", "polite")
FIVE_WORDS = (
    ONE_WORD.replace("scheduled", "planned")
    .replace("main", "large")
    .replace("Friday", "Thursday")
    .replace("folder", "drive")
)

# Family B: 75 words; one changed word gives 0.921. Prompt only (40 words), two changed words give 0.727.
SKY_PROMPT = (
    "Explain in simple terms why the sky looks blue during the day and turns red or orange around sunset "
    "when the sun is low on the horizon, and say whether the same effect happens on other planets with an atmosphere."
)
SKY_RESPONSE = (
    "Air scatters blue light much more than red, so daytime skies look blue, while light from a low sun "
    "crosses more air and reaches you reddened; Mars shows the reverse because its dust scatters red."
)
SKY_ONE_WORD = SKY_PROMPT.replace("simple", "plain")
SKY_TWO_WORDS = SKY_ONE_WORD.replace("horizon", "skyline")

BASE = {"id": "sft-001", "prompt": PROMPT, "response": RESPONSE}
SHOUTED = {"id": "sft-002", "prompt": PROMPT.upper().replace(" ", "  "), "response": RESPONSE}
NEAR = {"id": "sft-003", "prompt": ONE_WORD, "response": RESPONSE}
FAR = {"id": "sft-004", "prompt": FIVE_WORDS, "response": RESPONSE}
PERU = {"id": "sft-005", "prompt": "What is the capital of Peru?", "response": "Lima."}
CHAT_BASE = {
    "id": "sft-006",
    "messages": [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": RESPONSE}],
}
SKY = {"id": "sft-007", "prompt": SKY_PROMPT, "response": SKY_RESPONSE}
SKY_NEAR = {"id": "sft-008", "prompt": SKY_ONE_WORD, "response": SKY_RESPONSE}

EVAL_PERU = {"id": "eval-1", "prompt": "What is the capital of Peru?", "response": "Lima."}
EVAL_SKY = {"prompt": SKY_PROMPT, "response": SKY_RESPONSE}  # no id: known as line:<n>


def line(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode()


def write_jsonl(path: Path, *objs: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line(obj) + b"\n" for obj in objs))
    return path


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


def outcomes(
    *objs: object,
    kind: RecordKind = "sft",
    config: DedupStageSpec | None = None,
    reference: ContentIndex | None = None,
) -> list[Outcome]:
    numbered = [(number, obj if isinstance(obj, bytes) else line(obj)) for number, obj in enumerate(objs, start=1)]
    return list(dedup_lines(numbered, kind=kind, config=config or DedupStageSpec(), reference=reference))


def dropped(outcome: Outcome) -> tuple[str, str, float]:
    assert isinstance(outcome, Dropped), outcome
    return outcome.reason, outcome.match, round(outcome.similarity, 4)


# ---------------------------------------------------------------------------
# ContentIndex


def test_index_prefers_an_exact_match_and_falls_back_to_near() -> None:
    index = ContentIndex(near=NearDuplicateSpec())
    index.add("a", index.probe(f"{PROMPT}\n{RESPONSE}"))
    index.add("b", index.probe("something else entirely"))

    exact = index.lookup(index.probe(f"{PROMPT}\n{RESPONSE}"))
    near = index.lookup(index.probe(f"{ONE_WORD}\n{RESPONSE}"))
    assert exact is not None and (exact.key, exact.similarity, exact.exact) == ("a", 1.0, True)
    assert near is not None and (near.key, near.exact) == ("a", False)
    assert near.similarity == pytest.approx(63 / 69)
    assert index.lookup(index.probe("nothing like it")) is None
    assert len(index) == 2


def test_index_without_near_only_finds_exact_matches() -> None:
    index = ContentIndex(algorithm="sha256")
    probe = index.probe(f"{PROMPT}\n{RESPONSE}")
    assert len(probe.digest) == 64 and probe.fingerprint is None
    index.add("a", probe)
    assert index.lookup(index.probe(f"{ONE_WORD}\n{RESPONSE}")) is None
    assert index.lookup(index.probe(f"{PROMPT}\n{RESPONSE}")) is not None
    assert not index.near_enabled


def test_index_keeps_the_first_key_of_a_digest() -> None:
    index = ContentIndex()
    index.add("first", index.probe("same"))
    index.add("second", index.probe("same"))
    hit = index.lookup(index.probe("same"))
    assert hit is not None and hit.key == "first"
    assert len(index) == 2


def test_index_refuses_a_probe_without_a_fingerprint() -> None:
    plain, near = ContentIndex(), ContentIndex(near=NearDuplicateSpec())
    with pytest.raises(ValueError, match="no fingerprint"):
        near.add("a", plain.probe("text"))


# ---------------------------------------------------------------------------
# dedup_lines: exact and near duplicates within the input


def test_exact_duplicates_after_normalisation_keep_the_first() -> None:
    first, second, third = outcomes(BASE, SHOUTED, CHAT_BASE)
    assert isinstance(first, Kept)
    assert (first.line, first.record_id, first.data) == (1, "sft-001", line(BASE) + b"\n")
    assert dropped(second) == ("exact_duplicate", "sft-001", 1.0)
    assert dropped(third) == ("exact_duplicate", "sft-001", 1.0)  # chat form joins to the same text


def test_near_duplicates_are_verified_with_the_exact_jaccard() -> None:
    base, near, far = outcomes(BASE, NEAR, FAR)
    assert isinstance(base, Kept)
    assert dropped(near) == ("near_duplicate", "sft-001", 0.913)
    assert isinstance(far, Kept)  # 0.63 is below the 0.8 threshold


def test_matches_are_against_kept_records_only() -> None:
    # A copy of a dropped near duplicate is a near duplicate of the kept record, not an exact copy of the dropped one.
    near_copy = NEAR | {"id": "sft-009", "prompt": ONE_WORD.upper()}
    [_, near, copy] = outcomes(BASE, NEAR, near_copy)
    assert dropped(near) == ("near_duplicate", "sft-001", 0.913)
    assert dropped(copy) == ("near_duplicate", "sft-001", 0.913)


def test_near_detection_can_be_switched_off() -> None:
    config = DedupStageSpec(near=NearDuplicateSpec(enabled=False))
    base, shouted, near = outcomes(BASE, SHOUTED, NEAR, config=config)
    assert isinstance(base, Kept)
    assert dropped(shouted) == ("exact_duplicate", "sft-001", 1.0)
    assert isinstance(near, Kept)


def test_punctuation_stripping_turns_a_near_match_into_an_exact_one() -> None:
    a = {"id": "a", "prompt": "Hello, world!", "response": "Hi."}
    b = {"id": "b", "prompt": "hello world", "response": "hi"}
    [_, default] = outcomes(a, b)
    [_, stripped] = outcomes(a, b, config=DedupStageSpec(normalize=NormalizeSpec(strip_punctuation=True)))
    assert dropped(default) == ("near_duplicate", "a", 1.0)  # tokens ignore punctuation, hashes do not
    assert dropped(stripped) == ("exact_duplicate", "a", 1.0)


def test_fields_restrict_what_is_compared() -> None:
    same_prompt = {"id": "sft-009", "prompt": PROMPT, "response": "A completely different reply."}
    [_, both] = outcomes(BASE, same_prompt)
    [_, prompt_only] = outcomes(BASE, same_prompt, config=DedupStageSpec(fields=["prompt"]))
    assert isinstance(both, Kept)
    assert dropped(prompt_only) == ("exact_duplicate", "sft-001", 1.0)


def test_preference_records_compare_all_three_texts_by_default() -> None:
    pair = {"id": "p1", "prompt": PROMPT, "chosen": RESPONSE, "rejected": "No."}
    other_rejected = {"id": "p2", "prompt": PROMPT, "chosen": RESPONSE, "rejected": "Never."}
    chat = {
        "id": "p3",
        "prompt": [{"role": "user", "content": PROMPT}],
        "chosen": [{"role": "assistant", "content": RESPONSE}],
        "rejected": [{"role": "assistant", "content": "No."}],
    }
    [_, near, exact] = outcomes(pair, other_rejected, chat, kind="preference")
    assert dropped(near) == ("near_duplicate", "p1", 0.9706)  # the rejected text differs, so not exact
    assert dropped(exact) == ("exact_duplicate", "p1", 1.0)  # chat form joins to the same three texts

    exact_only = DedupStageSpec(near=NearDuplicateSpec(enabled=False))
    [_, kept] = outcomes(pair, other_rejected, kind="preference", config=exact_only)
    assert isinstance(kept, Kept)
    [_, by_prompt] = outcomes(pair, other_rejected, kind="preference", config=DedupStageSpec(fields=["prompt"]))
    assert dropped(by_prompt) == ("exact_duplicate", "p1", 1.0)


def test_blank_lines_are_skipped_and_ids_fall_back_to_content_hashes() -> None:
    anonymous = {"prompt": PROMPT, "response": RESPONSE}
    [kept, duplicate] = outcomes(b"", anonymous, b"   ", anonymous)
    assert isinstance(kept, Kept)
    assert (kept.line, len(kept.record_id)) == (2, 16)
    assert dropped(duplicate) == ("exact_duplicate", kept.record_id, 1.0)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b'{"prompt": "q"}', "line 2 is not a valid sft record (response: Field required [missing]); run the validate"),
        (b"[1, 2]", "line 2 is not a JSON object"),
        (b"{broken", "line 2 is not valid JSON"),
        (b'{"a": 1, "a": 2}', "line 2 is not valid JSON: duplicate key 'a'"),
        (b'{"prompt": "caf\xe9", "response": "x"}', "line 2 is not valid UTF-8"),
    ],
)
def test_unusable_input_lines_raise_stage_input_error(content: bytes, message: str) -> None:
    with pytest.raises(StageInputError) as excinfo:
        outcomes(BASE, content)
    error = excinfo.value
    assert message in str(error)
    assert (error.stage, error.line, error.exit_code) == ("dedup", 2, 65)


# ---------------------------------------------------------------------------
# Reference sets and contamination


def test_reference_lines_may_be_records_or_plain_objects(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "eval.jsonl",
        EVAL_PERU,
        EVAL_SKY,
        {"id": 7, "prompt": "Seven", "response": "seven", "split": "test"},
        {"id": "chat", "messages": [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": RESPONSE}]},
    )
    path.write_bytes(path.read_bytes() + b"\n   \n")  # blank lines are skipped
    index = load_reference(path, kind="sft", config=DedupStageSpec())
    assert len(index) == 4
    keys = []
    for text in ("what is the capital of peru?\nlima.", "seven\nseven"):  # normalised, fields joined by newline
        hit = index.lookup(index.probe(text))
        assert hit is not None and hit.exact
        keys.append(hit.key)
    assert keys == ["eval-1", "7"]

    [_, sky, chat] = outcomes(BASE, SKY, CHAT_BASE, reference=index)
    assert dropped(sky) == ("contaminated", "line:2", 1.0)
    assert dropped(chat) == ("contaminated", "chat", 1.0)


@pytest.mark.parametrize(
    ("obj", "message"),
    [
        (
            {"prompt": "only a prompt"},
            "line 1 of {path} is neither a sft record nor an object with string fields prompt, response",
        ),
        ({"prompt": PROMPT, "response": ["not", "text"]}, "neither a sft record nor an object"),
        ("just a string", "line 1 of {path} is not a JSON object"),
    ],
)
def test_unusable_reference_lines_raise_stage_input_error(tmp_path: Path, obj: object, message: str) -> None:
    path = write_jsonl(tmp_path / "eval.jsonl", obj)
    with pytest.raises(StageInputError, match=message.format(path=path).replace("(", "\\(").replace(")", "\\)")):
        load_reference(path, kind="sft", config=DedupStageSpec())


def test_contamination_is_checked_before_duplicates(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "eval.jsonl", EVAL_PERU, EVAL_SKY)
    config = DedupStageSpec(reference=ReferenceSpec(path=path))
    index = load_reference(path, kind="sft", config=config)

    result = outcomes(BASE, PERU, SKY_NEAR, SHOUTED, PERU | {"id": "sft-010"}, reference=index, config=config)

    assert isinstance(result[0], Kept)
    assert dropped(result[1]) == ("contaminated", "eval-1", 1.0)
    assert dropped(result[2]) == ("contaminated", "line:2", 0.9211)
    assert dropped(result[3]) == ("exact_duplicate", "sft-001", 1.0)
    # A copy of a contaminated record is contaminated too, not a duplicate of a record that was never kept.
    assert dropped(result[4]) == ("contaminated", "eval-1", 1.0)


def test_reference_fields_and_threshold_have_their_own_settings(tmp_path: Path) -> None:
    # Prompt-only comparison with two changed words: 0.727, below the default 0.8 but above 0.5.
    path = write_jsonl(tmp_path / "eval.jsonl", {"id": "eval-a", "prompt": SKY_PROMPT, "answer": "not compared"})
    strict = DedupStageSpec(reference=ReferenceSpec(path=path, fields=["prompt"]))
    loose = DedupStageSpec(reference=ReferenceSpec(path=path, fields=["prompt"], threshold=0.5))
    record = {"id": "sft-020", "prompt": SKY_TWO_WORDS, "response": "Something unrelated to the eval answer."}

    [kept] = outcomes(record, reference=load_reference(path, kind="sft", config=strict), config=strict)
    [hit] = outcomes(record, reference=load_reference(path, kind="sft", config=loose), config=loose)
    assert isinstance(kept, Kept)
    assert dropped(hit) == ("contaminated", "eval-a", 0.7273)


def test_reference_check_works_with_near_detection_off(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "eval.jsonl", EVAL_SKY)
    config = DedupStageSpec(near=NearDuplicateSpec(enabled=False), reference=ReferenceSpec(path=path))
    index = load_reference(path, kind="sft", config=config)
    [near_hit, exact_hit] = outcomes(SKY_NEAR, SKY, reference=index, config=config)
    assert dropped(near_hit) == ("contaminated", "line:1", 0.9211)  # the reference still uses MinHash
    assert dropped(exact_hit) == ("contaminated", "line:1", 1.0)


# ---------------------------------------------------------------------------
# run_dedup: files, report, determinism, errors, logging


@pytest.fixture
def train(tmp_path: Path) -> Path:
    return write_jsonl(tmp_path / "validated.jsonl", BASE, SHOUTED, NEAR, FAR, PERU, CHAT_BASE, SKY, SKY_NEAR)


@pytest.fixture
def evaluation(tmp_path: Path) -> Path:
    return write_jsonl(tmp_path / "eval.jsonl", EVAL_PERU, EVAL_SKY)


def test_run_writes_kept_rejected_and_cluster_files(train: Path, evaluation: Path, tmp_path: Path) -> None:
    config = DedupStageSpec(reference=ReferenceSpec(path=evaluation))
    report = run_dedup(train, tmp_path / "out", kind="sft", config=config)

    assert (report.total, report.kept, report.dropped) == (8, 2, 6)
    assert report.dropped_fraction == pytest.approx(0.75)
    assert report.reasons == {"contaminated": 3, "exact_duplicate": 2, "near_duplicate": 1}
    assert (report.groups, report.reference_size) == (1, 2)
    assert read_jsonl(report.output_path) == [BASE, FAR]

    rejects = read_jsonl(report.rejects_path)
    assert [(r["line"], r["id"], r["reason"], r["match"], r["similarity"]) for r in rejects] == [
        (2, "sft-002", "exact_duplicate", "sft-001", 1.0),
        (3, "sft-003", "near_duplicate", "sft-001", 0.913043),
        (5, "sft-005", "contaminated", "eval-1", 1.0),
        (6, "sft-006", "exact_duplicate", "sft-001", 1.0),
        (7, "sft-007", "contaminated", "line:2", 1.0),
        (8, "sft-008", "contaminated", "line:2", 0.921053),
    ]
    assert rejects[1]["record"] == NEAR

    assert read_jsonl(report.clusters_path) == [
        {
            "kept": "sft-001",
            "size": 4,
            "members": [
                {"id": "sft-002", "reason": "exact_duplicate", "similarity": 1.0},
                {"id": "sft-003", "reason": "near_duplicate", "similarity": 0.913043},
                {"id": "sft-006", "reason": "exact_duplicate", "similarity": 1.0},
            ],
        }
    ]


def test_report_paths_and_dict(train: Path, tmp_path: Path) -> None:
    config = DedupStageSpec(output_file="kept.jsonl", rejects_file="dropped.jsonl", clusters_file="groups.jsonl")
    report = run_dedup(train, tmp_path / "out", kind="sft", config=config)

    assert report.output_path == tmp_path / "out" / "kept.jsonl"
    assert report.rejects_path == tmp_path / "out" / "dropped.jsonl"
    assert report.clusters_path == tmp_path / "out" / "groups.jsonl"
    payload = report.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    # Without a reference set the sky pair is an ordinary near-duplicate group.
    assert (payload["stage"], payload["total"], payload["kept"], payload["dropped"]) == ("dedup", 8, 4, 4)
    assert (payload["reference_size"], payload["groups"]) == (None, 2)
    assert payload["reasons"] == {"exact_duplicate": 2, "near_duplicate": 2}
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["dropped.jsonl", "groups.jsonl", "kept.jsonl"]


def test_two_runs_with_the_same_seed_are_byte_identical(train: Path, evaluation: Path, tmp_path: Path) -> None:
    config = DedupStageSpec(reference=ReferenceSpec(path=evaluation), near=NearDuplicateSpec(seed=7))
    first = run_dedup(train, tmp_path / "a", kind="sft", config=config)
    second = run_dedup(train, tmp_path / "b", kind="sft", config=config)
    again = run_dedup(train, tmp_path / "a", kind="sft", config=config)

    for path in ("output_path", "rejects_path", "clusters_path"):
        content = getattr(first, path).read_bytes()
        assert content == getattr(second, path).read_bytes() == getattr(again, path).read_bytes()
    assert first.to_dict() == again.to_dict()
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == [
        "dedup_clusters.jsonl",
        "dedup_rejects.jsonl",
        "deduped.jsonl",
    ]


def test_missing_input_or_reference_fails_before_creating_outputs(train: Path, tmp_path: Path) -> None:
    with pytest.raises(InputFileError):
        run_dedup(tmp_path / "absent.jsonl", tmp_path / "out", kind="sft")
    with pytest.raises(InputFileError):
        config = DedupStageSpec(reference=ReferenceSpec(path=tmp_path / "no-eval.jsonl"))
        run_dedup(train, tmp_path / "out", kind="sft", config=config)
    assert not (tmp_path / "out").exists()


def test_refuses_to_overwrite_its_input_or_the_reference_set(train: Path, evaluation: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="would overwrite"):
        run_dedup(train, tmp_path, kind="sft", config=DedupStageSpec(output_file="validated.jsonl"))
    config = DedupStageSpec(reference=ReferenceSpec(path=evaluation), rejects_file="eval.jsonl")
    with pytest.raises(ConfigError, match="would overwrite"):
        run_dedup(train, tmp_path, kind="sft", config=config)
    assert len(read_jsonl(train)) == 8 and len(read_jsonl(evaluation)) == 2


def test_an_unusable_line_leaves_no_partial_outputs(tmp_path: Path) -> None:
    source = write_jsonl(tmp_path / "raw.jsonl", BASE, {"prompt": "no response"})
    with pytest.raises(StageInputError, match="line 2 of"):
        run_dedup(source, tmp_path / "out", kind="sft")
    assert list((tmp_path / "out").iterdir()) == []


def test_empty_input_writes_empty_files_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = write_jsonl(tmp_path / "validated.jsonl")
    with caplog.at_level(logging.INFO, logger="curator"):
        report = run_dedup(source, tmp_path / "out", kind="sft")
    assert (report.total, report.kept, report.dropped_fraction) == (0, 0, 0.0)
    assert report.output_path.read_bytes() == b""
    assert report.clusters_path.read_bytes() == b""
    assert "dedup.empty_input" in [r.getMessage() for r in caplog.records]


def test_json_logs_carry_the_stage_and_the_summary(
    train: Path, evaluation: Path, tmp_path: Path, json_log: JsonLogLines
) -> None:
    config = DedupStageSpec(reference=ReferenceSpec(path=evaluation))
    run_dedup(train, tmp_path / "out", kind="sft", config=config)

    lines = json_log()
    assert {entry["stage"] for entry in lines} == {"dedup"}
    [loaded] = [entry for entry in lines if entry["event"] == "dedup.reference_loaded"]
    assert (loaded["records"], loaded["fields"]) == (2, ["prompt", "response"])
    dropped_events = [entry for entry in lines if entry["event"] == "dedup.record_dropped"]
    assert [(e["line"], e["reason"], e["match"]) for e in dropped_events][:2] == [
        (2, "exact_duplicate", "sft-001"),
        (3, "near_duplicate", "sft-001"),
    ]
    assert all("record" not in entry for entry in dropped_events)
    [finished] = [entry for entry in lines if entry["event"] == "dedup.finished"]
    assert (finished["level"], finished["kept"], finished["reasons"]["contaminated"]) == ("info", 2, 3)

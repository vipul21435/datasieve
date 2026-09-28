"""The pipeline spec: a YAML, TOML or JSON file describing one curation run.

Example (YAML)::

    version: 1
    name: support-sft
    input:
      path: data/raw.jsonl        # relative paths resolve against the spec file's directory
      kind: sft                   # sft | preference
    output:
      dir: out/support-sft        # optional; default <CURATOR_WORK_DIR>/<name>
    stages:
      - stage: validate
        unknown_fields: metadata  # reject (default) | metadata
        max_invalid_fraction: 0.2 # fail the run if more than 20% of records are quarantined
      - stage: dedup
        near:
          threshold: 0.8          # Jaccard similarity of word n-grams that makes a near duplicate
        reference:
          path: data/eval.jsonl   # drop records that match this set (contamination)

Unknown keys are errors everywhere, so a typo such as ``max_invalid_fracton``
fails loudly instead of silently using a default. Duplicate keys are errors in
every format. ``stages`` is a list of entries tagged by ``stage``; later stage
types (PII, quality, ...) join the :data:`StageSpec` union.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from collections.abc import Hashable
from pathlib import Path
from pathlib import PurePath
from typing import Annotated
from typing import Final
from typing import Literal
from typing import Self

import yaml
from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import ValidationInfo
from pydantic import model_validator
from pydantic_core import PydanticCustomError

from curator.dedup.text import HashAlgorithm
from curator.errors import SpecError
from curator.io.jsonl import StrictJSONError
from curator.io.jsonl import loads_strict
from curator.schemas.fields import TEXT_FIELDS
from curator.schemas.fields import TextField
from curator.schemas.issues import issue_from_error
from curator.schemas.parse import RecordKind
from curator.schemas.parse import UnknownFieldPolicy

SPEC_VERSION: Final = 1


def _resolve_against_spec_dir(path: Path, info: ValidationInfo) -> Path:
    base_dir = info.context.get("base_dir") if isinstance(info.context, dict) else None
    if base_dir is None or path.is_absolute():
        return path
    return Path(base_dir) / path


def _plain_file_name(value: str) -> str:
    if value in {"", ".", ".."} or PurePath(value).name != value or "\\" in value:
        raise PydanticCustomError(
            "file_name", "must be a plain file name without directories, got '{value}'", {"value": value}
        )
    return value


SpecPath = Annotated[Path, AfterValidator(_resolve_against_spec_dir)]
"""A path; when the spec is loaded from a file, relative paths are resolved against that file's directory."""

FileName = Annotated[str, AfterValidator(_plain_file_name)]
"""A file name inside the run's output directory."""


class SpecModel(BaseModel):
    """Base for spec sections: unknown keys are errors and parsed specs are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InputSpec(SpecModel):
    path: SpecPath
    """The input dataset."""

    kind: RecordKind
    """What the records are: ``sft`` or ``preference``."""

    format: Literal["jsonl"] = "jsonl"


class OutputSpec(SpecModel):
    dir: SpecPath | None = None
    """Where the run writes its files. Defaults to ``<CURATOR_WORK_DIR>/<pipeline name>``."""


class ValidateStageSpec(SpecModel):
    """Schema validation. Valid records go to ``output_file``, the rest to ``quarantine_file``."""

    stage: Literal["validate"] = "validate"

    unknown_fields: UnknownFieldPolicy = "reject"
    """Top-level keys the schema does not define: ``reject`` the record, or move them into ``metadata``."""

    reject_duplicate_ids: bool = True
    """Quarantine a record whose id (explicit, or content-derived) was already seen in this input."""

    max_invalid_fraction: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    """Fail the stage when more than this fraction of records is quarantined. ``None`` never fails."""

    output_file: FileName = "validated.jsonl"
    quarantine_file: FileName = "quarantine.jsonl"

    @model_validator(mode="after")
    def _distinct_files(self) -> Self:
        if self.output_file == self.quarantine_file:
            raise PydanticCustomError("same_file", "output_file and quarantine_file must be different files")
        return self


def _unique_fields(fields: list[TextField]) -> list[TextField]:
    if len(set(fields)) != len(fields):
        raise PydanticCustomError("duplicate_field", "each field may be listed once, got {fields}", {"fields": fields})
    return fields


TextFields = Annotated[list[TextField], Field(min_length=1), AfterValidator(_unique_fields)]
"""Which text fields of a record to compare, in order (see :data:`curator.schemas.fields.TEXT_FIELDS`)."""


class NormalizeSpec(SpecModel):
    """How text is canonicalised before exact matching."""

    lowercase: bool = True
    collapse_whitespace: bool = True
    strip_punctuation: bool = False
    """Drop Unicode punctuation and symbols, so ``Hello, world!`` equals ``hello world``."""


class NearDuplicateSpec(SpecModel):
    """MinHash/LSH near-duplicate detection over word n-grams."""

    enabled: bool = True
    num_perm: Annotated[int, Field(ge=16, le=1024)] = 128
    """Signature length; more permutations make the LSH candidate step more accurate but slower."""

    ngram_size: Annotated[int, Field(ge=1, le=16)] = 3
    """Words per shingle."""

    threshold: Annotated[float, Field(gt=0.0, le=1.0)] = 0.8
    """Jaccard similarity of two records' shingle sets at or above which the later one is a duplicate."""

    seed: int = 42
    """Seed of the MinHash permutations; the same seed always gives the same clusters."""


class ReferenceSpec(SpecModel):
    """A set the input must not overlap with, typically an evaluation set (contamination check)."""

    path: SpecPath
    """JSONL; lines are records of the pipeline's kind, or plain objects with the text fields as strings."""

    fields: TextFields | None = None
    """Fields compared between input and reference records. Defaults to the stage's ``fields``."""

    threshold: Annotated[float, Field(gt=0.0, le=1.0)] | None = None
    """Near-match threshold against the reference. Defaults to ``near.threshold``."""


class DedupStageSpec(SpecModel):
    """Exact and near-duplicate removal (keep-first), plus an optional contamination check.

    Kept records go to ``output_file``, dropped ones to ``rejects_file`` with
    their reason, and ``clusters_file`` lists each group of duplicates.
    """

    stage: Literal["dedup"] = "dedup"

    fields: TextFields | None = None
    """Text fields that make up a record's content. Defaults to every text field of the record kind."""

    normalize: NormalizeSpec = NormalizeSpec()
    hash: HashAlgorithm = "xxhash"
    """Hash of the normalised text used for exact matching."""

    near: NearDuplicateSpec = NearDuplicateSpec()
    reference: ReferenceSpec | None = None

    output_file: FileName = "deduped.jsonl"
    rejects_file: FileName = "dedup_rejects.jsonl"
    clusters_file: FileName = "dedup_clusters.jsonl"

    @model_validator(mode="after")
    def _distinct_files(self) -> Self:
        names = [self.output_file, self.rejects_file, self.clusters_file]
        if len(set(names)) != len(names):
            raise PydanticCustomError(
                "same_file", "output_file, rejects_file and clusters_file must be different files"
            )
        return self

    def fields_for(self, kind: RecordKind) -> tuple[TextField, ...]:
        """The fields compared for duplicates: ``fields``, or every text field of ``kind``."""
        return tuple(self.fields) if self.fields is not None else TEXT_FIELDS[kind]

    def reference_fields_for(self, kind: RecordKind) -> tuple[TextField, ...]:
        """The fields compared against the reference set: ``reference.fields``, else :meth:`fields_for`."""
        if self.reference is not None and self.reference.fields is not None:
            return tuple(self.reference.fields)
        return self.fields_for(kind)


StageSpec = Annotated[ValidateStageSpec | DedupStageSpec, Field(discriminator="stage")]
"""One entry of ``stages``, selected by its ``stage`` key."""


class PipelineSpec(SpecModel):
    version: Literal[1]
    """Spec format version, so that later formats can be told apart."""

    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=64)]
    """Pipeline name; lower-case letters, digits, ``.``, ``_`` and ``-`` (it can name a directory)."""

    description: str = ""
    input: InputSpec
    output: OutputSpec = OutputSpec()
    stages: Annotated[list[StageSpec], Field(min_length=1)]

    @model_validator(mode="after")
    def _stage_order(self) -> Self:
        names = [stage.stage for stage in self.stages]
        if names[0] != "validate":
            raise PydanticCustomError(
                "validate_not_first", "the first stage must be 'validate' so later stages get typed records"
            )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise PydanticCustomError(
                "duplicate_stage", "each stage may appear once; repeated: {stages}", {"stages": ", ".join(duplicates)}
            )
        for stage in self.stages:
            if isinstance(stage, DedupStageSpec):
                self._check_text_fields(stage)
        self._check_distinct_files()
        return self

    def _check_distinct_files(self) -> None:
        # Each stage reads the previous stage's output, so two stages writing the same file
        # would clobber the pipeline's own intermediate data.
        writers: dict[str, str] = {}
        for index, stage in enumerate(self.stages):
            for name, value in stage:
                if not name.endswith("_file"):
                    continue
                where = f"stages[{index}].{name}"
                if value in writers:
                    raise PydanticCustomError(
                        "same_file",
                        "{where} and {other} both write '{file}'; every stage output needs its own file",
                        {"where": where, "other": writers[value], "file": value},
                    )
                writers[value] = where

    def _check_text_fields(self, stage: DedupStageSpec) -> None:
        allowed = TEXT_FIELDS[self.input.kind]
        for where, fields in (
            ("fields", stage.fields),
            ("reference.fields", stage.reference and stage.reference.fields),
        ):
            unknown = [field for field in fields or () if field not in allowed]
            if unknown:
                raise PydanticCustomError(
                    "unknown_text_field",
                    "{where} lists {unknown}, but {kind} records only have {allowed}",
                    {
                        "where": where,
                        "unknown": ", ".join(unknown),
                        "kind": self.input.kind,
                        "allowed": ", ".join(allowed),
                    },
                )

    def output_dir(self, work_dir: Path) -> Path:
        """The run's output directory: ``output.dir`` if set, else ``<work_dir>/<name>``."""
        return self.output.dir if self.output.dir is not None else work_dir / self.name


# ---------------------------------------------------------------------------
# Loading


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """PyYAML's safe loader, except that duplicate mapping keys are an error instead of "last one wins"."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, object]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load_yaml(text: str) -> object:
    return yaml.load(text, Loader=_UniqueKeySafeLoader)  # noqa: S506 - a SafeLoader subclass


def _load_toml(text: str) -> object:
    return tomllib.loads(text)


def _load_json(text: str) -> object:
    return loads_strict(text)


_LOADERS: Final[dict[str, Callable[[str], object]]] = {
    ".yaml": _load_yaml,
    ".yml": _load_yaml,
    ".toml": _load_toml,
    ".json": _load_json,
}
SPEC_SUFFIXES: Final = tuple(_LOADERS)


def _spec_problems(exc: ValidationError) -> list[str]:
    """One line per validation error, e.g. ``stages[0].output_file: must be a plain file name ... [file_name]``.

    Every error inside a ``stages`` entry passes through the discriminated
    union, so pydantic puts the entry's tag in the path
    (``stages[0].validate.output_file``). The tag repeats the entry's own
    ``stage`` key, so it is dropped.
    """
    problems = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = error["loc"]
        if len(loc) >= 3 and loc[0] == "stages" and isinstance(loc[1], int):
            error = {**error, "loc": loc[:2] + loc[3:]}
        problems.append(issue_from_error(error).describe())
    return problems


def parse_spec(data: object, *, base_dir: Path | None = None, source: Path | None = None) -> PipelineSpec:
    """Validate already-decoded spec data.

    ``base_dir`` anchors relative paths; ``source`` only improves error messages.
    """
    where = f" in {source}" if source is not None else ""
    if not isinstance(data, dict):
        raise SpecError(f"invalid pipeline spec{where}: the top level must be a mapping", path=source)
    try:
        return PipelineSpec.model_validate(data, context={"base_dir": base_dir})
    except ValidationError as exc:
        raise SpecError(f"invalid pipeline spec{where}", path=source, problems=_spec_problems(exc)) from None


def load_spec(path: str | Path) -> PipelineSpec:
    """Read and validate a pipeline spec file (``.yaml``/``.yml``, ``.toml`` or ``.json``).

    Relative paths inside the spec resolve against the file's directory, so a
    spec works the same from any working directory. Every failure raises
    :class:`~curator.errors.SpecError`.
    """
    path = Path(path)
    loader = _LOADERS.get(path.suffix.lower())
    if loader is None:
        raise SpecError(
            f"unsupported pipeline spec format {path.suffix or '(none)'!r}; use one of {', '.join(SPEC_SUFFIXES)}",
            path=path,
        )
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SpecError(f"pipeline spec not found: {path}", path=path) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecError(f"cannot read pipeline spec {path}: {exc}", path=path) from exc
    try:
        data = loader(text)
    except (yaml.YAMLError, tomllib.TOMLDecodeError, json.JSONDecodeError, StrictJSONError) as exc:
        raise SpecError(f"cannot parse pipeline spec {path}: {exc}", path=path) from exc
    return parse_spec(data, base_dir=path.parent, source=path)


__all__ = [
    "SPEC_SUFFIXES",
    "SPEC_VERSION",
    "DedupStageSpec",
    "InputSpec",
    "NearDuplicateSpec",
    "NormalizeSpec",
    "OutputSpec",
    "PipelineSpec",
    "ReferenceSpec",
    "SpecModel",
    "StageSpec",
    "TextFields",
    "ValidateStageSpec",
    "load_spec",
    "parse_spec",
]

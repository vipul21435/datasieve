Forked from https://github.com/ChenghaoMou/text-dedup.

# DataSieve

Validate and deduplicate LLM training data (SFT and preference records) from one declarative
pipeline spec, on CPU, with no API keys. DataSieve is a fork of
[ChenghaoMou/text-dedup](https://github.com/ChenghaoMou/text-dedup): the upstream `text_dedup`
package is kept intact and reused for MinHash/LSH, and everything the fork adds lives in the new
`curator` package next to it.

```text
$ make demo
pipeline sft-demo-300: examples/data/sft_demo.jsonl -> .curator/sft-demo-300/deduped.jsonl
  validate      299 in ->    287 out   (12 quarantined: blank_text=2, consecutive_same_role=1, ...)
  dedup         287 in ->    220 out   (67 dropped: contaminated=12, exact_duplicate=30, near_duplicate=25)
finished in 0.06s (4,648 records/s)
```

## What I built on top

Everything below is fork work (`git log --author=vipul21435@iiitd.ac.in`); `src/text_dedup/`,
`benchmarks/` and `report/` are upstream code.

- **Typed configuration** (`curator.config`): `CURATOR_*` settings via pydantic-settings and a
  YAML/TOML/JSON pipeline spec in which unknown keys, duplicate keys and typos are errors, with
  relative paths resolved against the spec file.
- **Strict record schemas** (`curator.schemas`): SFT in chat or prompt/response form and
  preference (chosen/rejected) pairs, with stable machine-readable reason codes and
  content-derived ids for records that have none.
- **The `validate` stage** (`curator.stages.validate`): every input line ends up in the output or
  in a quarantine file with all of its reasons; an invalid-fraction threshold fails the run
  before a half-good dataset flows downstream; outputs are atomic and byte-identical on re-run.
- **The `dedup` stage** (`curator.stages.dedup`, `curator.dedup`): exact dedup by content hash of
  normalised text, near-duplicate clustering with MinHash/LSH (seeded, keep-first, every
  candidate verified with the exact Jaccard similarity) and a contamination check against an
  evaluation split; rejects carry the reason, the matched id and the similarity.
- **A pipeline runner and CLI** (`curator.pipeline`, `curator.cli`): stages chain through the
  run directory and `python -m curator run` prints the stage funnel or a JSON report.
- **Structured logging and an error hierarchy** (`curator.log`, `curator.errors`): JSON log lines
  with bound context, and `CuratorError` subclasses with stable codes and `sysexits` exit codes.
- **Packaging and delivery**: a seeded 299-record demo dataset with planted defects, a Makefile,
  a digest-pinned non-root Docker image with a compose file, and CI that lints, type-checks,
  tests on Python 3.12 and 3.13, checks the wheel and builds the image.

## Architecture

```mermaid
flowchart LR
    subgraph inputs [Inputs]
        SPEC["pipeline spec<br/>(YAML / TOML / JSON)"]
        RAW["raw JSONL<br/>(sft | preference)"]
        EVAL["reference split<br/>(optional)"]
    end
    CLI["python -m curator run"] --> LOAD["curator.config<br/>load_settings + load_spec"]
    SPEC --> LOAD
    LOAD --> RUN["curator.pipeline.run_pipeline"]
    RAW --> V
    subgraph stages [Stages, each reads the previous output]
        V["validate<br/>curator.schemas"] --> D["dedup<br/>hash + MinHash/LSH"]
    end
    RUN --> V
    EVAL --> D
    V -.-> Q["quarantine.jsonl"]
    D -.-> R["dedup_rejects.jsonl<br/>dedup_clusters.jsonl"]
    D --> OUT["deduped.jsonl"]
    D --> TD["text_dedup (upstream)<br/>hashing, n-grams, LSH params"]
    RUN --> REPORT["stage funnel / JSON report<br/>+ JSON logs on stderr"]
```

## Quickstart

Five commands from a fresh clone; needs Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/vipul21435/datasieve.git
cd datasieve
uv sync
make demo
make test
```

`make demo` validates and deduplicates the bundled sample (see the output above) and writes the
kept records, the quarantine file, the rejects and the clusters to `.curator/sft-demo-300/`.
`make test` runs the 670-odd unit tests and doctests with coverage. `make help` lists the rest
(`lint`, `typecheck`, `ci`, `clean`).

## CLI reference

```text
python -m curator run   SPEC [--work-dir DIR] [--json] [--log-level LEVEL] [--log-format json|text]
python -m curator check SPEC                  [--log-level LEVEL] [--log-format json|text]
python -m curator --version
```

| Command | What it does |
| --- | --- |
| `run SPEC` | Runs every stage of the spec in order and prints the stage funnel to stdout. `--json` prints the full report instead (`PipelineReport.to_dict()` plus `elapsed_seconds` and `records_per_second`). `--work-dir` overrides `CURATOR_WORK_DIR`. |
| `check SPEC` | Loads and validates the spec, prints its input and stages, runs nothing. |

Logs go to stderr in the format chosen by `--log-format` or `CURATOR_LOG_FORMAT`. Exit codes:
`0` success, `2` usage error, `78` bad spec or settings (`ConfigError`), `65` unusable data
(`DataError`, including a quarantine threshold breach), `66` missing input file.

The same thing from Python:

```python
from curator.config import load_settings, load_spec
from curator.log import configure_logging
from curator.pipeline import run_pipeline

settings = load_settings()
configure_logging(settings.log_level, settings.log_format)
report = run_pipeline(load_spec("examples/sft-demo.yaml"), work_dir=settings.work_dir)
print(report.output_path, [stage.to_dict() for stage in report.stages])
```

## Pipeline spec reference

A run is one spec file. Unknown or duplicate keys are errors, relative paths resolve against the
spec's directory, and the first stage must be `validate` so later stages see typed records. Every
option with its default:

```yaml
version: 1
name: sft-demo-300                 # [a-z0-9._-]; names the output directory
description: ""
input:
  path: data/sft_demo.jsonl
  kind: sft                        # sft | preference
output:
  dir: null                        # default: <CURATOR_WORK_DIR>/<name>
stages:
  - stage: validate
    unknown_fields: reject         # reject | metadata (move unknown top-level keys into metadata)
    reject_duplicate_ids: true
    max_invalid_fraction: null     # e.g. 0.25: fail the run if more than 25% is quarantined
    output_file: validated.jsonl
    quarantine_file: quarantine.jsonl
  - stage: dedup
    fields: null                   # text fields to compare; default: all of the kind (sft: prompt, response)
    normalize:
      lowercase: true
      collapse_whitespace: true
      strip_punctuation: false
    hash: xxhash                   # xxhash | sha256, over the normalised text
    near:
      enabled: true
      num_perm: 128                # MinHash signature length
      ngram_size: 3                # word n-grams
      threshold: 0.8               # Jaccard similarity at or above which the later record is dropped
      seed: 42
    reference: null                # optional contamination check:
    #   path: data/eval.jsonl      #   records matching this split are dropped
    #   fields: null               #   default: the stage's fields
    #   threshold: null            #   default: near.threshold
    output_file: deduped.jsonl
    rejects_file: dedup_rejects.jsonl
    clusters_file: dedup_clusters.jsonl
```

**Records.** SFT accepts `{"messages": [{"role": ..., "content": ...}, ...]}` (user first,
assistant last, optional leading system turn) or `{"prompt", "response", "system"?}`; preference
accepts `{"prompt", "chosen", "rejected"}` in text or chat form, with optional scores. Both take an
optional `id` and a free-form `metadata` object. A record without an id gets a content-derived
one, so the chat and prompt/response forms of one example share an id.

**Validate.** Each non-blank line goes to `validated.jsonl` or to `quarantine.jsonl` with every
reason (`blank_text`, `missing`, `string_type`, `consecutive_same_role`, `duplicate_id`,
`invalid_json`, `duplicate_key`, `non_finite_number`, ...).

**Dedup.** The compared fields are normalised, joined and checked keep-first in this order:
`contaminated` (matches the reference split exactly or at the threshold), `exact_duplicate`
(content hash already kept), `near_duplicate` (a kept record's n-gram set is at least
`threshold` similar; LSH proposes candidates, the exact Jaccard similarity decides). Rejects and
clusters look like this:

```json
{"line": 3, "id": "sft-103", "reason": "near_duplicate", "match": "sft-101", "similarity": 0.928571, "record": {"id": "sft-103", "prompt": "...", "response": "..."}}
{"kept": "sft-101", "size": 4, "members": [{"id": "sft-102", "reason": "exact_duplicate", "similarity": 1.0}, {"id": "sft-103", "reason": "near_duplicate", "similarity": 0.928571}]}
```

More examples: [`examples/sft-validate.yaml`](examples/sft-validate.yaml),
[`examples/preference-validate.toml`](examples/preference-validate.toml) and
[`examples/sft-dedup.yaml`](examples/sft-dedup.yaml) (ten records, every dedup reason once).

## Settings, logging and errors

| Variable | Default | Meaning |
| --- | --- | --- |
| `CURATOR_LOG_LEVEL` | `INFO` | Minimum log level |
| `CURATOR_LOG_FORMAT` | `json` | `json` (one object per line on stderr) or `text` |
| `CURATOR_WORK_DIR` | `.curator` | Output root; a spec without `output.dir` writes to `<work dir>/<name>` |

CLI flags override environment variables, which override a `.env` file (see
[`.env.example`](.env.example)), which overrides the defaults; an unknown `CURATOR_*` variable is
logged as a likely typo. With `json`, every log line is an event plus fields, with the pipeline
and stage bound as context:

```json
{"ts":"2026-09-28T21:50:11.520Z","level":"info","logger":"curator.stages.dedup","event":"dedup.finished","pipeline":"sft-demo-300","stage":"dedup","total":287,"kept":220,"dropped":67,"reasons":{"contaminated":12,"exact_duplicate":30,"near_duplicate":25},"groups":55}
```

Every deliberate failure is a `curator.errors.CuratorError` with a stable `code`, a `to_dict()`
for API bodies and logs, and an exit code from `sysexits.h` (see the CLI reference).

## Sample output

`python -m curator run examples/sft-demo.yaml --json --log-level WARNING` (paths shortened):

```json
{
  "pipeline": "sft-demo-300",
  "input": "examples/data/sft_demo.jsonl",
  "output": ".curator/sft-demo-300/deduped.jsonl",
  "stages": [
    {"stage": "validate", "total": 299, "valid": 287, "invalid": 12, "invalid_fraction": 0.040134,
     "reasons": {"blank_text": 2, "consecutive_same_role": 1, "duplicate_id": 1, "duplicate_key": 1,
                 "invalid_json": 2, "missing": 1, "non_finite_number": 1, "string_type": 2, "too_short": 1}},
    {"stage": "dedup", "total": 287, "kept": 220, "dropped": 67, "dropped_fraction": 0.233449,
     "reasons": {"contaminated": 12, "exact_duplicate": 30, "near_duplicate": 25},
     "groups": 55, "reference_size": 15}
  ],
  "elapsed_seconds": 0.064709,
  "records_per_second": 4620.7
}
```

The demo data is generated by [`examples/make_demo_data.py`](examples/make_demo_data.py) with a
fixed seed: 220 distinct records plus 30 exact copies (case or whitespace changes), 25 near copies
(one or two words changed), 12 copies of the 15-record evaluation split and 12 invalid rows. The
counts the pipeline reports are exactly the planted ones, and a test regenerates the files byte
for byte.

## Docker

```bash
docker build -t datasieve:dev .
docker run --rm datasieve:dev                                   # the demo pipeline
docker run --rm datasieve:dev --help
docker run --rm -v "$PWD/data:/data:ro" datasieve:dev run /data/my-spec.yaml
docker compose run --rm demo                                    # same, via docker-compose.yml
```

The image is `python:3.12-slim` pinned by digest, runs as the non-root user `curator`, installs
only the runtime dependencies from `uv.lock` and does not build the optional Rust suffix-array
backend. It is `linux/amd64` only: the upstream dependency `polars-grouper` publishes x86_64
wheels but no aarch64 wheel, so an arm64 image would need a Rust toolchain; on Apple Silicon Docker
runs it under emulation, which is fine for the demo and small datasets.

## Design decisions and tradeoffs

- **Fork, do not vendor.** `text_dedup` stays a normal importable package and `curator` reuses its
  hashing, n-gram and LSH-parameter code. The cost is carrying upstream's dependency list
  (`datasets`, `polars`, `scipy`) in an image that only needs a fraction of it.
- **Spec file, not flags.** A pipeline is data (versioned, diffable, reviewable), and pydantic
  models with `extra="forbid"` make typos loud. The tradeoff is verbosity for one-off runs, which
  is why the CLI stays thin.
- **Validate first, always.** Later stages only ever see typed records, so they hold no defensive
  code, and the quarantine file makes every rejection auditable. A dataset that fails the
  invalid-fraction threshold produces no output file at all rather than a partial one.
- **Keep-first, exact-verified near dedup.** LSH proposes, Jaccard decides: the reported
  similarity is real, the seed fixes the clusters, and re-runs are byte-identical. The cost is a
  full n-gram comparison per candidate pair, and the usual LSH S-curve means a pair right at the
  threshold is found with only even odds, so thresholds should sit below the similarity to catch.
- **Streaming, single process.** Records stream line by line and only the index lives in memory,
  so a laptop handles millions of short records; there is no multi-process path yet.
- **Stable codes everywhere.** Reason codes, error codes, exit codes and log event names are part
  of the interface and covered by tests, so shell pipelines and dashboards can branch on them.

## Benchmarks

Measured on this machine on 2026-09-29: Apple Silicon Mac (M2, 8 cores, 8 GB RAM), Python 3.12,
`uv run python -m curator run <spec> --log-level ERROR`, wall-clock as reported by the CLI. Each
row is one `validate -> dedup` run (`num_perm=128`, 3-grams, threshold 0.8); the 22k set was
generated with the demo generator's templates (2,000 planted copies) and, because the templates
share most of their words, the near-duplicate count exceeds the planted one.

| Dataset | Records in | Kept | Runs | Wall-clock | Throughput |
| --- | --- | --- | --- | --- | --- |
| `examples/sft-demo.yaml` (bundled) | 299 | 220 | 3 | 0.060-0.065 s | 4,600-5,000 records/s |
| 22k templated SFT records (not bundled) | 22,000 | 18,512 | 3 | 4.0-5.7 s | 3,800-5,500 records/s |

`make demo` end to end (including interpreter start-up) takes 0.46 s (`/usr/bin/time -p`). The
22k runs overlapped a Docker image build on the same machine, hence the spread. Full test suite:
672 tests in about 13 s, 84% line coverage (`make test`).

## What I would do next

- A SQLite run ledger keyed by spec hash and input digest, so re-running an unchanged pipeline is a
  no-op and each output can be traced to its inputs.
- PII and secret scrubbing as a stage (emails, phone numbers, API keys), with the same
  quarantine-or-rewrite semantics as `validate`.
- A quality validator registry (length bounds, language, refusal and boilerplate detection) that
  specs can compose per field.
- A seeded synthetic data generator behind the same schemas, so a pipeline can be exercised at any
  size, plus a report and dataset card written next to the output.
- A Typer CLI with rich progress and a FastAPI job service that runs specs asynchronously and
  serves the reports.
- Hermetic CI (no PyPI access after `uv sync --frozen`) and property-based tests with `hypothesis`
  for the normaliser and the schemas.

## Development

```bash
make install     # uv sync
make lint        # ruff check + ruff format --check
make typecheck   # mypy over src/ (stricter for curator.*)
make test        # pytest with coverage: tests/ plus src doctests
make ci          # lint, typecheck, test
uv run pre-commit install   # optional hygiene hooks; the upstream justfile is also kept
```

`text_dedup.suffix_array` shells out to Google's
[deduplicate-text-datasets](https://github.com/google-research/deduplicate-text-datasets) (Rust,
git submodule under `third_party/`). Nothing in the curation pipeline or the tests needs it; to
enable it run `git submodule update --init third_party/deduplicate-text-datasets` and install a
Rust toolchain. Ruff, mypy and pytest never enter `third_party/`.

## Repository layout

| Path | Contents | Origin |
| --- | --- | --- |
| `src/curator/` | `cli.py`, `pipeline.py`, `config/`, `schemas/`, `stages/`, `dedup/`, `io/`, `errors.py`, `log.py` | this fork |
| `examples/` | Pipeline specs, the seeded demo generator and sample datasets with planted defects | this fork |
| `Makefile`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/main.yml` | Developer entry points, runtime image, CI | this fork (CI extended from upstream) |
| `src/text_dedup/` | MinHash, SimHash, Bloom filter and suffix-array dedup | upstream text-dedup |
| `benchmarks/`, `report/`, `configs/`, `justfile` | Upstream benchmark harness, report app and configs | upstream text-dedup |
| `third_party/deduplicate-text-datasets` | Optional Rust suffix-array backend (git submodule) | Google Research |

Both `curator` and `text_dedup` ship in the single `sft-data-curator` wheel.

## License and credits

Apache 2.0, see [LICENSE](LICENSE); the upstream copyright is kept. `text_dedup`, the benchmarks
and the report app are the work of Chenghao Mou and the text-dedup contributors, whose
[README](https://github.com/ChenghaoMou/text-dedup#readme) and
[documentation](https://chenghaomou.github.io/text-dedup/) describe the upstream algorithms and
their own benchmark results. Cite upstream when you use the deduplication algorithms:

```bibtex
@software{chenghao_mou_2023_8364980,
  author       = {Chenghao Mou and
                  Chris Ha and
                  Kenneth Enevoldsen and
                  Peiyuan Liu},
  title        = {ChenghaoMou/text-dedup: Reference Snapshot},
  month        = sep,
  year         = 2023,
  publisher    = {Zenodo},
  version      = {2023.09.20},
  doi          = {10.5281/zenodo.8364980},
  url          = {https://doi.org/10.5281/zenodo.8364980}
}
```

Upstream acknowledges [Datasketch](https://github.com/ekzhu/datasketch) (MIT),
[simhash-py](https://github.com/seomoz/simhash-py) and [simhash-cpp](https://github.com/seomoz/simhash-cpp)
(MIT), [deduplicate-text-datasets](https://github.com/google-research/deduplicate-text-datasets)
(Apache 2.0) and [Gaoya](https://github.com/serega/gaoya) (MIT).

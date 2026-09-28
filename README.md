Forked from https://github.com/ChenghaoMou/text-dedup.

# sft-data-curator

Training-data curation for SFT and preference (chosen/rejected) datasets, built as a fork of
[ChenghaoMou/text-dedup](https://github.com/ChenghaoMou/text-dedup). The upstream `text_dedup`
package is kept intact and reused for near-duplicate detection; the fork's own code lives in the
new `curator` package. Everything runs on CPU and needs no paid API keys.

> Status: configuration, record schemas, the `validate` stage and the `dedup` stage are in place
> and run end to end through `curator.pipeline` (see [Running a pipeline](#running-a-pipeline)).
> PII scrubbing, quality filters, synthetic data, dataset cards, the run ledger, the CLI and the
> HTTP API land in follow-up changes.

## What I built on top

- Typed configuration: `CURATOR_*` settings and a YAML/TOML/JSON pipeline spec where unknown or
  duplicate keys are errors (`curator.config`).
- Strict record schemas for SFT and preference data with stable reason codes (`curator.schemas`).
- The `validate` stage: per-record reasons, a quarantine file, an invalid-fraction threshold and
  atomic, re-run-stable outputs (`curator.stages.validate`).
- JSON logging with bound context and an error hierarchy with `sysexits` codes (`curator.log`,
  `curator.errors`).
- The `dedup` stage: exact-hash dedup on normalised text, MinHash/LSH near-duplicate clustering
  (keep-first, seeded, verified with the exact Jaccard similarity) and a contamination check
  against a reference split; rejects carry a reason code, the matched id and the similarity
  (`curator.stages.dedup`, `curator.dedup`).
- A pipeline runner that chains the stages of a spec and summarises every stage
  (`curator.pipeline`).

## Repository layout

| Path | Contents | Origin |
| --- | --- | --- |
| `src/curator/` | Curation pipeline: `config/`, `schemas/`, `stages/`, `dedup/`, `io/`, `pipeline.py`, `errors.py`, `log.py` | this fork |
| `examples/` | Example pipeline specs and small sample datasets with deliberate defects | this fork |
| `src/text_dedup/` | MinHash, SimHash, Bloom filter and suffix-array dedup | upstream text-dedup |
| `benchmarks/`, `report/` | Dedup benchmarks and report app | upstream text-dedup |
| `third_party/deduplicate-text-datasets` | Optional Rust suffix-array backend (git submodule) | Google Research |

Both `curator` and `text_dedup` ship in the single `sft-data-curator` wheel.

## Validating a dataset

A run is described by a pipeline spec in YAML, TOML or JSON. Relative paths resolve against the
spec file's directory, and unknown or duplicate keys are errors, so a typo cannot silently fall
back to a default.

```yaml
# examples/sft-validate.yaml
version: 1
name: sft-demo
input:
  path: data/sft_sample.jsonl
  kind: sft                    # sft | preference
stages:
  - stage: validate
    unknown_fields: metadata   # reject (default) | metadata
    max_invalid_fraction: 0.5  # fail the run if more than half of the records are rejected
```

There is no CLI yet, so run the spec from Python (see [Running a pipeline](#running-a-pipeline));
a single stage can also be called directly:

```python
from curator.config import load_settings, load_spec
from curator.log import configure_logging
from curator.stages.validate import run_validate

settings = load_settings()
configure_logging(settings.log_level, settings.log_format)
spec = load_spec("examples/sft-validate.yaml")
report = run_validate(
    spec.input.path, spec.output_dir(settings.work_dir), kind=spec.input.kind, config=spec.stages[0]
)
print(report.to_dict())  # total 14, valid 8, rejected records per reason code
```

**Accepted records.** Each JSONL line is one record, with an optional `id` and a free-form
`metadata` object:

| Kind | Shape | Fields |
| --- | --- | --- |
| `sft` | chat | `messages`: `system` (first message only), then alternating `user` / `assistant` turns, ending with `assistant`; `tool` turns follow an assistant turn |
| `sft` | prompt/response | `prompt`, `response`, optional `system` |
| `preference` | text | `prompt`, `chosen`, `rejected`, optional `score_chosen` / `score_rejected` |
| `preference` | chat | `prompt` (messages ending with a user turn), `chosen` / `rejected` (assistant continuations), optional scores |

Validation is strict. Types are never coerced and text is never modified. A preference pair is
rejected when `chosen` and `rejected` are identical apart from whitespace, or when `chosen` is
scored below `rejected`. A record without an `id` gets a content hash as its id, which ignores
metadata and scores and is the same for the chat and flat forms of an example. Exact duplicates
are caught by that id.

**Outputs.** `validated.jsonl` holds the valid records in their input shape, with `id` filled in.
`quarantine.jsonl` has one entry per rejected line, listing every reason:

```json
{"line": 4, "id": "sft-004", "reasons": [{"code": "blank_text", "field": "messages[1].content", "message": "must contain non-whitespace text"}], "record": {"id": "sft-004", "messages": ["..."]}}
```

Reason codes are stable. They are pydantic's error types (`missing`, `extra_forbidden`,
`literal_error`, ...), the schema rules (`blank_text`, `last_turn_not_assistant`,
`consecutive_same_role`, `identical_responses`, `chosen_scored_below_rejected`, ...), or file-level
checks (`invalid_json`, `invalid_encoding`, `duplicate_key`, `non_finite_number`,
`invalid_unicode`, `duplicate_id`). Both files are written atomically, so a re-run produces
byte-identical output. When the rejected fraction exceeds `max_invalid_fraction`, the stage
raises `QuarantineThresholdError`. It still writes the quarantine file, but it withholds
`validated.jsonl`.

## Deduplicating a dataset

The `dedup` stage runs after `validate` and removes duplicates keep-first: the first record of a
group stays, later ones are dropped, and the output keeps the input's order. Every option with its
default:

```yaml
stages:
  - stage: validate
  - stage: dedup
    fields: [prompt, response]  # text fields to compare; default: every text field of the kind
    normalize:                  # canonical form used for exact matching
      lowercase: true
      collapse_whitespace: true
      strip_punctuation: false
    hash: xxhash                # content hash of the normalised text: xxhash | sha256
    near:
      enabled: true
      num_perm: 128             # MinHash signature length
      ngram_size: 3             # word n-grams
      threshold: 0.8            # Jaccard similarity at or above which the later record is a duplicate
      seed: 42                  # same seed, same clusters
    reference:                  # optional contamination check
      path: data/eval.jsonl     # records matching this set are dropped
      fields: [prompt]          # default: the stage's fields
      threshold: 0.9            # default: near.threshold
    output_file: deduped.jsonl
    rejects_file: dedup_rejects.jsonl
    clusters_file: dedup_clusters.jsonl
```

For each record the compared fields are normalised, joined with newlines (so field boundaries
count) and checked in this order:

1. `contaminated`: the text matches a record of `reference.path`, exactly or at
   `reference.threshold`. Reference lines may be records of the pipeline's kind or plain objects
   with the compared fields as strings; a line's id is its `id`, else `line:<number>`.
2. `exact_duplicate`: the content hash of the normalised text was already kept. This also catches
   the chat and prompt/response forms of one example, and copies that differ only in case,
   whitespace or metadata.
3. `near_duplicate`: a kept record's word n-gram set is at least `near.threshold` similar.
   MinHash/LSH (upstream `text_dedup`'s 64-bit scheme and band split) proposes candidate pairs and
   the exact Jaccard similarity decides, so the reported similarity is never an estimate. A pair
   sitting right at the threshold is proposed with only even odds (the usual LSH S-curve), so set
   the threshold a little below the similarity you want to catch.

Groups are stars around the kept record: a record that resembles a dropped duplicate but not the
kept one stays, and a copy of a contaminated record is contaminated too, since nothing of it was
kept. Contaminated records are therefore not clustered.

**Outputs.** `deduped.jsonl` holds the kept records unchanged. `dedup_rejects.jsonl` has one entry
per dropped record with the id it matched (a kept record, or the reference record for
`contaminated`); `dedup_clusters.jsonl` has one entry per kept record that had duplicates:

```json
{"line": 3, "id": "sft-103", "reason": "near_duplicate", "match": "sft-101", "similarity": 0.928571, "record": {"id": "sft-103", "prompt": "...", "response": "..."}}
{"kept": "sft-101", "size": 4, "members": [{"id": "sft-102", "reason": "exact_duplicate", "similarity": 1.0}, {"id": "sft-103", "reason": "near_duplicate", "similarity": 0.928571}, {"id": "sft-104", "reason": "exact_duplicate", "similarity": 1.0}]}
```

All three files are written atomically, and two runs with the same seed produce byte-identical
files. A line that is not a valid record raises `StageInputError` (run `validate` first), and an
output that would overwrite the input or the reference file raises `ConfigError`.

## Running a pipeline

`curator.pipeline.run_pipeline` runs a spec's stages in order; each stage reads the previous
stage's output, and every file lands in `output.dir` or `<CURATOR_WORK_DIR>/<name>`:

```python
from curator.config import load_settings, load_spec
from curator.log import configure_logging
from curator.pipeline import run_pipeline

settings = load_settings()
configure_logging(settings.log_level, settings.log_format)
report = run_pipeline(load_spec("examples/sft-dedup.yaml"), work_dir=settings.work_dir)
print(report.output_path)  # .curator/sft-dedup-demo/deduped.jsonl
for stage in report.to_dict()["stages"]:
    print(stage)
```

On [`examples/sft-dedup.yaml`](examples/sft-dedup.yaml) that prints two summaries: `validate`
keeps 9 of 10 records (one blank response), and `dedup` keeps 3 of those 9, dropping three exact
duplicates (an upper-cased copy, the chat form of the same example, and a copy that differs only
in metadata), one near duplicate (0.93) and two records contaminated by the evaluation split (an
exact copy and a one-word variant at 0.95). A stage that fails raises its `CuratorError`; earlier
stages keep their files and later ones do not run.

## Settings, logging and errors

Runtime settings come from `CURATOR_*` environment variables or a `.env` file; see
[`.env.example`](.env.example). Explicit arguments override environment variables, which override
`.env`, which overrides the defaults. An unknown `CURATOR_*` variable is logged as a likely typo.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CURATOR_LOG_LEVEL` | `INFO` | Minimum log level |
| `CURATOR_LOG_FORMAT` | `json` | `json` (one object per line on stderr) or `text` |
| `CURATOR_WORK_DIR` | `.curator` | Output root; a spec without `output.dir` writes to `<work dir>/<name>` |

With `json`, every log line is an event name plus fields, with context such as the current stage
bound by `curator.log.log_context` (the pipeline runner also binds `pipeline`; file paths left
out here):

```json
{"ts":"2026-09-28T17:59:11.134Z","level":"info","logger":"curator.stages.validate","event":"validate.finished","stage":"validate","total":14,"valid":8,"invalid":6,"reasons":{"blank_text":1,"duplicate_id":2,"invalid_json":1,"last_turn_not_assistant":1,"literal_error":1}}
```

Every deliberate failure is a `curator.errors.CuratorError`. Each one has a stable `code`, a
`to_dict()` for API bodies, and an exit code taken from `sysexits.h`. `ConfigError` (bad settings
or spec) exits with 78, `DataError` (a rejected record, too many rejects, a stage input that is
not a record) with 65, and `InputFileError` (missing input) with 66.

## Development

Requires [uv](https://docs.astral.sh/uv/); Python 3.12 is selected via `.python-version`.

```bash
uv sync                                  # runtime + dev dependencies, pinned by uv.lock
uv run pytest                            # unit tests and src doctests
uv run ruff check . && uv run ruff format --check .
uv run mypy                              # src/, with stricter settings for curator.*
uv run pre-commit install                # optional: ruff + hygiene hooks on commit
```

### Optional: suffix-array backend

`text_dedup.suffix_array` shells out to Google's
[deduplicate-text-datasets](https://github.com/google-research/deduplicate-text-datasets) (Rust).
It is not needed to install the package, run the tests or use the curation pipeline, and a plain
clone leaves the submodule empty. To enable it:

```bash
git submodule update --init third_party/deduplicate-text-datasets
# and install a Rust toolchain so that `cargo` is on PATH (https://rustup.rs)
```

Without both, the suffix-array algorithm stops before doing any work with a
`SuffixArrayBackendError` that names what is missing.

The checkout is vendored upstream code: ruff excludes `third_party/` (see `[tool.ruff]` in
`pyproject.toml`), so the lint and format commands above neither flag nor rewrite it, and the
default pytest and mypy runs never enter it.

## License and credits

Apache 2.0, see [LICENSE](LICENSE). `text_dedup`, the benchmarks and the report app are the work
of Chenghao Mou and the text-dedup contributors; cite upstream (see Citations below) when you use
the deduplication algorithms.

---

# Upstream README: text-dedup

The rest of this file is the upstream project's README, kept as-is. Its install and run
instructions refer to the upstream repository.

<center><img src="./banner.png"/ style="background-color:white;"></center>

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![GitHub](https://img.shields.io/github/license/ChenghaoMou/text-dedup) [![Codacy Badge](https://app.codacy.com/project/badge/Grade/cc66178e49d24908ac1fb2b2dbe4e5b3)](https://www.codacy.com/gh/ChenghaoMou/text-dedup/dashboard?utm_source=github.com&utm_medium=referral&utm_content=ChenghaoMou/text-dedup&utm_campaign=Badge_Grade) [![Codacy Badge](https://app.codacy.com/project/badge/Coverage/cc66178e49d24908ac1fb2b2dbe4e5b3)](https://www.codacy.com/gh/ChenghaoMou/text-dedup/dashboard?utm_source=github.com&utm_medium=referral&utm_content=ChenghaoMou/text-dedup&utm_campaign=Badge_Coverage) [![DOI](https://zenodo.org/badge/347428086.svg)](https://zenodo.org/badge/latestdoi/347428086)

## Installation

```bash
git clone https://github.com/ChenghaoMou/text-dedup
cd text-dedup
uv sync
```

## Documentation

[Github Pages](https://chenghaomou.github.io/text-dedup/)

## Features

This repository contains a collection of text deduplication scripts that are ready to use, or modify based on your needs:

- MinHash + MinHashLSH for near-duplicate detection
- 64 or 128 bit SimHash
- SuffixArray Substring exact deduplication
- Bloom Filter exact deduplication

All algorithms use a config-based approach with TOML files for easy customization.

## Quick Start

All deduplication scripts read from a `config.toml` file in the project root.

### 1. Configure your settings

Edit `config.toml` with your input data and algorithm settings:

<details>
<summary>MinHash Near Deduplication</summary>

```toml
[input]
input_type = "local_files"
file_type = "parquet"

[input.read_arguments]
path = "data/your_data"
split = "train"

[algorithm]
algorithm_name = "minhash"
text_column = "text"
seed = 42
batch_size = 10000
num_perm = 240
threshold = 0.7
false_positive_weight = 0.5
false_negative_weight = 0.5
hash_bits = 64
ngram_size = 5
check_false_positive = true

[output]
output_dir = "output"
clean_cache = false
save_clusters = true

[debug]
enable_profiling = false
```

</details>

<details>
<summary>SimHash Near Deduplication</summary>

```toml
[input]
input_type = "local_files"
file_type = "parquet"

[input.read_arguments]
path = "data/your_data"
split = "train"

[algorithm]
algorithm_name = "simhash"
text_column = "text"
hash_bits = 64
ngram_size = 3
bit_diff = 3

[output]
output_dir = "output"
clean_cache = false

[debug]
enable_profiling = false
```

</details>

<details>
<summary>Bloom Filter Exact Deduplication</summary>

```toml
[input]
input_type = "local_files"
file_type = "parquet"

[input.read_arguments]
path = "data/your_data"
split = "train"

[algorithm]
algorithm_name = "bloom_filter"
text_column = "text"
error_rate = 1e-5
expected_elements = 100000

[output]
output_dir = "output"
clean_cache = false

[debug]
enable_profiling = false
```

</details>

<details>
<summary>Suffix Array Substring Exact Deduplication</summary>

```toml
[input]
input_type = "local_files"
file_type = "parquet"

[input.read_arguments]
path = "data/your_data"
split = "train"

[algorithm]
algorithm_name = "suffix_array"
text_column = "text"
google_repo_path = "third_party/deduplicate-text-datasets"
merge_strategy = "longest"
length_threshold = 100
cache_dir = ".cache"

[output]
output_dir = "output"
clean_cache = false

[debug]
enable_profiling = false
```

</details>

### 2. Run the deduplication

```bash
# MinHash
python -m text_dedup.minhash

# SimHash
python -m text_dedup.simhash

# Bloom Filter
python -m text_dedup.bloom_filter

# Suffix Array
python -m text_dedup.suffix_array
```

## Benchmarks

<details>
<summary>pinecone/core-2020-05-10-deduplication</summary>

| Algorithm                       | Precision (Duplicates) | Recall (Duplicates) | Precision (Non Duplicates) | Recall (Non Duplicates) | Macro F1 score |   Accuracy | Time    |
| :------------------------------ | ---------------------: | ------------------: | -------------------------: | ----------------------: | -------------: | ---------: | :------ |
| MinHash                         |                 0.9587 |              0.9416 |                     0.9450 |                  0.9611 |     **0.9518** | **0.9277** | 11.09s  |
| SimHash                         |                 0.9038 |              0.7323 |                     0.7993 |                  0.9318 |         0.8515 |     0.8375 | 626.11s |
| Exact Title Matching [^1]       |                  0.830 |                0.50 |                      0.709 |                   0.992 |          0.757 |      0.746 | -       |
| Simhash Matching [^1]           |                  0.697 |               0.247 |                      0.598 |                   0.985 |          0.631 |      0.616 | -       |
| Document Vector Similarity [^1] |                  0.912 |               0.779 |                      0.861 |                   0.986 |          0.885 |      0.883 | -       |
| Hybrid Method [^1]              |                  0.908 |               0.828 |                      0.899 |                   0.979 |          0.904 |      0.903 | -       |
| LaBSE[^2]                       |                  0.937 |               0.923 |                      0.930 |                   0.943 |          0.933 |      0.919 | -       |
| Multilingual USE[^2]            |                  0.917 |               0.907 |                      0.918 |                   0.927 |          0.917 |      0.909 | -       |
| Multilingual E5-Base[^2]        |                  0.931 |               0.908 |                      0.919 |                   0.939 |          0.924 |      0.920 | -       |
| MinHash + LSH[^2]               |                  0.929 |               0.902 |                      0.915 |                   0.938 |          0.921 |      0.918 | -       |
| RETSim Partial-Dup[^2]          |                  0.945 |               0.941 |                      0.945 |                   0.949 |          0.945 |      0.928 | -       |
| RETSim Near-Dup[^2]             |                  0.928 |               0.937 |                      0.942 |                   0.934 |          0.935 |      0.926 | -       |

</details>
<details>
<summary>NEWS-COPY</summary>

Adjusted Rand Index (ARI) on NEWS-COPY dataset:

| Model/Algorithm          | ARI       | Time    |
| :----------------------- | :-------- | :------ |
| MinHash                  | 0.7293    | 3.01s   |
| SimHash                  | 0.6463    | 140.03s |
| n-gram [^3]              | 0.440     | -       |
| SimHash[^2]              | 0.695     | -       |
| MinHash[^3]              | 0.737     | -       |
| MinHash[^2]              | 0.783     | -       |
| Multilingual USE[^2]     | 0.730     | -       |
| Multilingual E5-Base[^2] | 0.742     | -       |
| S-BERT[^3]               | 0.700     | -       |
| RETSim Partial-Dup[^2]   | 0.831     | -       |
| RETSim Near-Dup[^2]      | 0.704     | -       |
| Re-ranking [^3]          | **0.937** | -       |
| Bi-encoder [^3]          | 0.915     | -       |

</details>

### Running Benchmarks

You can reproduce the benchmark results using the provided benchmark suite.

#### Quick Start with Just

```bash
# Run all benchmarks (both datasets, all algorithms)
just benchmark-all

# Run only CORE dataset benchmarks
just benchmark-core

# Run only NEWS-COPY dataset benchmarks
just benchmark-news

# Run specific algorithm on specific dataset
just benchmark-core-minhash
just benchmark-core-simhash
just benchmark-news-minhash
just benchmark-news-simhash
```

#### Configuration Files

Benchmark configuration files are located in `configs/`:

- `benchmark_core_minhash.toml` - MinHash on CORE dataset
- `benchmark_core_simhash.toml` - SimHash on CORE dataset
- `benchmark_news_minhash.toml` - MinHash on NEWS-COPY dataset
- `benchmark_news_simhash.toml` - SimHash on NEWS-COPY dataset

To customize benchmark parameters, edit the config files and adjust hyperparameters like `num_perm`, `threshold`, `ngram_size`, or `bit_diff`.

[^1]: [Deduplication of Scholarly Documents using Locality Sensitive Hashing and Word Embeddings](https://aclanthology.org/2020.lrec-1.113)
[^2]: [RETSim: Resilient and Efficient Text Similarity](https://arxiv.org/abs/2311.17264)
[^3]: [Noise-Robust De-Duplication at Scale](https://www.semanticscholar.org/paper/Noise-Robust-De-Duplication-at-Scale-Silcock-D'Amico-Wong/7ca41cc5fc364b713aba5b573ae4ada801fd788a)

## License

[Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0.html)

## Citations

Generally, you can cite this repository as:

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

## Acknowledgements

This repository is inspired by the following projects, and is heavily influenced by lessons learned from my own participation in [BigScience (Apache 2.0)](https://github.com/bigscience-workshop) and [BigCode (Apache 2.0)](https://github.com/bigcode-project). There is a [blog post](https://publish.obsidian.md/chenghao/posts/20230220150602) about the journey. Feedbacks are welcome!

- [Datasketch](https://github.com/ekzhu/datasketch) (MIT)
- [simhash-py](https://github.com/seomoz/simhash-py/tree/master/simhash) and [simhash-cpp](https://github.com/seomoz/simhash-cpp) (MIT)
- [Deduplicating Training Data Makes Language Models Better](https://github.com/google-research/deduplicate-text-datasets) (Apache 2.0)
- [Gaoya](https://github.com/serega/gaoya) (MIT)

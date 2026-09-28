"""Generate the seeded demo dataset used by ``make demo`` (``examples/data/sft_demo*.jsonl``).

The dataset is small enough to read and large enough to time: templated
prompt/response pairs with planted defects whose counts are fixed by the seed:

* exact duplicates (same text with a different case or extra whitespace);
* near duplicates (one or two words changed, well above the 0.8 word-3-gram
  Jaccard threshold of the demo spec);
* contaminated records (copies or near copies of the evaluation split);
* invalid rows (blank responses, missing fields, a bad turn order, duplicate
  ids and lines that are not JSON).

Re-running the script rewrites both files byte for byte; a test checks that
the committed files match. Run it from anywhere::

    uv run python examples/make_demo_data.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20260929
DATA_DIR = Path(__file__).resolve().parent / "data"
TRAIN_PATH = DATA_DIR / "sft_demo.jsonl"
EVAL_PATH = DATA_DIR / "sft_demo_eval.jsonl"

UNIQUE = 220
"""Distinct valid training records."""
EXACT = 30
NEAR = 25
CONTAMINATED = 12
"""Six exact and six near copies of evaluation records."""
EVAL = 15
"""Records in the evaluation split."""

TONES = ["friendly", "formal", "concise", "playful", "reassuring", "matter-of-fact"]
FORMS = ["email", "announcement", "product description", "help-centre article", "checklist", "short guide"]
TOPICS = [
    "resetting a forgotten password",
    "the library's new late-return policy",
    "planting tomatoes on a shaded balcony",
    "preparing a bicycle for winter commuting",
    "choosing a first telescope",
    "hosting a neighbourhood repair cafe",
    "migrating a blog to a static site generator",
    "keeping a sourdough starter alive while travelling",
    "setting up a shared family calendar",
    "training for a first ten kilometre run",
    "reducing food waste in a small kitchen",
    "organising a school science fair",
]
AUDIENCES = ["new customers", "the whole team", "first-time volunteers", "busy parents", "students", "retirees"]
POINTS = [
    "what to do first",
    "the one mistake to avoid",
    "how long it usually takes",
    "what it costs",
    "when to ask for help",
    "how to know it worked",
    "which tools are needed",
    "what to prepare the day before",
]
OPENERS = ["Here is", "Below is", "This is"]
CLOSERS = [
    "Reply to this message if anything is unclear.",
    "Keep this note somewhere handy.",
    "Share it with anyone who might find it useful.",
    "That is all there is to it.",
]


def make_pair(rng: random.Random) -> tuple[str, str]:
    tone, form, topic = rng.choice(TONES), rng.choice(FORMS), rng.choice(TOPICS)
    audience = rng.choice(AUDIENCES)
    points = rng.sample(POINTS, 3)
    prompt = (
        f"Write a {tone} {form} about {topic} for {audience}. "
        f"Cover {points[0]}, {points[1]} and {points[2]}, and keep it under two hundred words."
    )
    response = (
        f"{rng.choice(OPENERS)} a {tone} {form} on {topic}, written for {audience}. "
        f"First, {points[0]}: start by reading the existing notes on the subject and writing down what you already know. "
        f"Second, {points[1]}: most people rush this part, so set aside an uninterrupted hour for it. "
        f"Third, {points[2]}: check it against the list above before you call it done. "
        f"{rng.choice(CLOSERS)}"
    )
    return prompt, response


def near_copy(prompt: str, response: str, rng: random.Random) -> tuple[str, str]:
    """Change one or two words so the pair stays well above the demo's 0.8 similarity threshold."""
    swaps = [("Write a", "Draft a"), ("keep it", "please keep it"), ("First,", "To begin,"), ("Third,", "Finally,")]
    for old, new in rng.sample(swaps, rng.choice([1, 2])):
        prompt, response = prompt.replace(old, new, 1), response.replace(old, new, 1)
    return prompt, response


def exact_copy(prompt: str, response: str, rng: random.Random) -> tuple[str, str]:
    """Same text after normalisation: a different case or extra whitespace."""
    variant = rng.choice(["upper", "spaces", "lower"])
    if variant == "upper":
        return prompt.upper(), response
    if variant == "lower":
        return prompt, response.lower()
    return prompt.replace(" ", "  "), response.replace(". ", ".  ")


def unique_pairs(rng: random.Random, count: int) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    while len(pairs) < count:
        prompt, response = make_pair(rng)
        if prompt not in seen:
            seen.add(prompt)
            pairs.append((prompt, response))
    return pairs


def invalid_lines(next_id: int) -> list[str]:
    """Rows the validate stage must quarantine, each with a different reason."""
    rows: list[object] = [
        {"id": f"demo-{next_id}", "prompt": "What is the capital of Portugal?", "response": "   "},
        {"id": f"demo-{next_id + 1}", "prompt": "Name three primary colours."},
        {
            "id": f"demo-{next_id + 2}",
            "messages": [{"role": "user", "content": "Hi"}, {"role": "user", "content": "?"}],
        },
        {"id": f"demo-{next_id + 3}", "prompt": "", "response": "An empty prompt is not a prompt."},
        {"id": "demo-1", "prompt": "Reuses the first record's id.", "response": "So it is quarantined."},
        {"id": f"demo-{next_id + 4}", "messages": [{"role": "assistant", "content": "I speak first."}]},
        {"id": f"demo-{next_id + 5}", "prompt": 42, "response": "The prompt must be text."},
        {"id": f"demo-{next_id + 6}", "prompt": "Response is null.", "response": None},
    ]
    lines = [json.dumps(row) for row in rows]
    lines.append('{"id": "demo-broken", "prompt": "This line is cut off in the middle')
    lines.append("not json at all")
    lines.append('{"id": "demo-dup-key", "prompt": "x", "prompt": "y", "response": "duplicate key"}')
    lines.append('{"id": "demo-inf", "prompt": "Score?", "response": "ten", "metadata": {"score": 1e999}}')
    return lines


def build() -> tuple[list[str], list[str]]:
    rng = random.Random(SEED)  # noqa: S311 - a fixed seed for reproducible sample data, not security
    pairs = unique_pairs(rng, UNIQUE + EVAL)
    train_pairs, eval_pairs = pairs[:UNIQUE], pairs[UNIQUE:]

    # Disjoint originals, so the reason counts do not depend on which copy comes first.
    sources = rng.sample(train_pairs, EXACT + NEAR)
    planted: list[tuple[str, str, str]] = []
    for prompt, response in sources[:EXACT]:
        planted.append((*exact_copy(prompt, response, rng), "exact"))
    for prompt, response in sources[EXACT:]:
        planted.append((*near_copy(prompt, response, rng), "near"))
    half = CONTAMINATED // 2
    for prompt, response in eval_pairs[:half]:
        planted.append((prompt, response, "contaminated-exact"))
    for prompt, response in eval_pairs[half:CONTAMINATED]:
        planted.append((*near_copy(prompt, response, rng), "contaminated-near"))

    records = [(prompt, response, "original") for prompt, response in train_pairs] + planted
    rng.shuffle(records)
    lines = [
        json.dumps({"id": f"demo-{index}", "prompt": prompt, "response": response, "source": source})
        for index, (prompt, response, source) in enumerate(records, start=1)
    ]
    bad = invalid_lines(len(lines) + 1)
    for line in bad:
        lines.insert(rng.randrange(len(lines) + 1), line)
    eval_lines = [
        json.dumps({"id": f"eval-{index}", "prompt": prompt, "response": response})
        for index, (prompt, response) in enumerate(eval_pairs, start=1)
    ]
    return lines, eval_lines


def main() -> None:
    train, evaluation = build()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRAIN_PATH.write_text("".join(line + "\n" for line in train), encoding="utf-8")
    EVAL_PATH.write_text("".join(line + "\n" for line in evaluation), encoding="utf-8")
    print(f"wrote {len(train)} lines to {TRAIN_PATH} and {len(evaluation)} lines to {EVAL_PATH}")


if __name__ == "__main__":
    main()

# Developer entry points. Every target runs through uv, so `make install` is the
# only prerequisite; the upstream justfile is kept for the text_dedup workflows.
.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test demo ci clean

DEMO_SPEC ?= examples/sft-demo.yaml

help: ## list the targets
	@grep -E '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{ printf "  %-10s %s\n", $$1, $$2 }'

install: ## create the virtual environment with all dependency groups
	uv sync

lint: ## ruff: lint and check formatting (no changes)
	uv run ruff check .
	uv run ruff format --check .

format: ## ruff: fix lint findings and reformat
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy over src/
	uv run mypy

test: ## pytest with coverage (unit tests + src doctests)
	uv run pytest --cov --cov-config=pyproject.toml --cov-report=term-missing --cov-report=xml

demo: ## validate + dedup the bundled 299-record SFT sample and print the stage funnel
	uv run python -m curator run $(DEMO_SPEC)

ci: lint typecheck test ## everything CI runs, in order

clean: ## remove caches, coverage output and demo output
	rm -rf .ruff_cache .mypy_cache .pytest_cache .coverage coverage.xml .curator dist
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -r {} +

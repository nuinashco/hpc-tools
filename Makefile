.PHONY: help setup check lint format typecheck test test-oldest validate

help:  ## show targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

setup:  ## create the environment and install the git hooks
	uv sync
	uv run pre-commit install --hook-type pre-commit --hook-type commit-msg

check: lint typecheck test test-oldest validate  ## everything a commit must pass

lint:  ## ruff lint + format check (no changes); the launchers have no .py to be found by
	uv run ruff check . plugins/*/bin/*
	uv run ruff format --check . plugins/*/bin/*

format:  ## ruff fixes + format
	uv run ruff check --fix . plugins/*/bin/*
	uv run ruff format . plugins/*/bin/*

typecheck:  ## pyright
	uv run pyright

test:  ## unit tests
	uv run pytest -q

test-oldest:  ## the same tests on Python 3.9, the one a Mac ships and the helpers must run on
	uv run --isolated --no-project --python 3.9 --with pytest pytest -q

validate:  ## Claude Code's own check of each plugin and of the marketplace
	@for plugin in plugins/*/; do claude plugin validate $$plugin --strict || exit 1; done
	claude plugin validate . --strict

.PHONY: install fmt lint typecheck test check build clean

install:
	uv sync --all-packages

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test

build:
	rm -rf dist
	uv build --package sbxloop-worker -o dist
	uv build --package sbxloop -o dist

clean:
	rm -rf dist .pytest_cache .mypy_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} +

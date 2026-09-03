.PHONY: install fmt lint typecheck security test test-cov check build clean

install:
	uv sync --all-packages

fmt:
	uv run ruff format .
	uv run ruff check --fix .
	uv run mdformat .

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mdformat --check .
	python3 scripts/check_self_references.py

typecheck:
	uv run mypy

security:
	uv run bandit -c pyproject.toml -r packages/sbxloop/src packages/sbxloop-worker/src

test:
	uv run pytest

test-cov:
	uv run pytest --cov=sbxloop --cov=sbxloop_worker --cov-report=term-missing --cov-fail-under=85

check: lint typecheck security test-cov

build:
	rm -rf dist
	uv build --package sbxloop-worker -o dist
	uv build --package sbxloop -o dist

clean:
	rm -rf dist .pytest_cache .mypy_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} +

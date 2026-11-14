.PHONY: check lint typecheck test format

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .
	@if command -v shellcheck >/dev/null; then shellcheck sandbox/*.sh sandbox/build/*.sh; else echo "shellcheck not installed; skipping"; fi

typecheck:
	uv run pyright

test:
	uv run pytest -q

format:
	uv run ruff check --fix .
	uv run ruff format .

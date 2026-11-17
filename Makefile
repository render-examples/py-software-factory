.PHONY: check lint typecheck test format

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .
	@command -v shellcheck >/dev/null || { echo "shellcheck is required (see docs/quickstart.md)" >&2; exit 1; }
	shellcheck sandbox/*.sh sandbox/build/*.sh

typecheck:
	uv run pyright

test:
	uv run pytest -q

format:
	uv run ruff check --fix .
	uv run ruff format .

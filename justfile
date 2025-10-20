test +args="":
    uv run pytest -vv --color=yes --showlocals --no-header '{{ args }}'

format:
    uv run ruff format .
    uv run ruff check . --fix || true

lint:
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright

install-deps:
    uv sync

update-deps: && install-deps
    uv lock --upgrade

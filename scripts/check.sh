#!/bin/sh
# Pre-commit check used during development: lint + tests, non-zero exit on any failure.
set -e
uv run ruff check src tests examples -q
uv run pytest -q > /tmp/evalplane-pytest.log 2>&1 || { tail -30 /tmp/evalplane-pytest.log; exit 1; }
tail -1 /tmp/evalplane-pytest.log

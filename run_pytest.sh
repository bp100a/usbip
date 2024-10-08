#!/bin/bash
# run pytest
export PYTHONPATH=./
PYTEST_ARGS=(-n auto -v --timeout=30 --cov --cov-branch --cov-config=.coveragerc)
python -m poetry run python -m pytest "${PYTEST_ARGS[@]}"

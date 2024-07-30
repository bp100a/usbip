#!/bin/bash
# run pytest
export PYTHONPATH=./
PYTEST_ARGS=(-n auto -v --timeout=30 --cov --cov-branch --cov-config=.coveragerc)
pytest "${PYTEST_ARGS[@]}"
pytest_exit_code=$?
# see what's in the folder
ls -als
coverage json -o .coverage/coverage-summary.json
exit "$pytest_exit_code"
